# ============================================================
# Discord Music + Moderation + Self Role + Welcome System
# Single-file main.py
#
# Environment Variables:
#   DISCORD_TOKEN
#   SPOTIFY_CLIENT_ID
#   SPOTIFY_CLIENT_SECRET
#
# Dependencies:
#   discord.py[voice]
#   Flask
#   yt-dlp
#   imageio-ffmpeg
#   spotipy
#   PyNaCl
#   davey
# ============================================================

import os
import re
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from flask import Flask, jsonify

import imageio_ffmpeg


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

DATABASE_FILE = "bot_data.sqlite3"

if not DISCORD_TOKEN:
    raise RuntimeError("ไม่พบ Environment Variable: DISCORD_TOKEN")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("DiscordBot")


# ============================================================
# TIMEZONE
# ============================================================

UTC = timezone.utc


# ============================================================
# DATABASE
# ============================================================

db_lock = asyncio.Lock()


def get_db():
    conn = sqlite3.connect(
        DATABASE_FILE,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = get_db()
    cur = conn.cursor()

    # --------------------------------------------------------
    # Guild settings
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,

            welcome_enabled INTEGER DEFAULT 0,
            welcome_channel_id INTEGER,
            welcome_message TEXT,
            welcome_image TEXT,
            welcome_embed INTEGER DEFAULT 1,

            goodbye_enabled INTEGER DEFAULT 0,
            goodbye_channel_id INTEGER,
            goodbye_message TEXT,
            goodbye_image TEXT,
            goodbye_embed INTEGER DEFAULT 1,

            boost_enabled INTEGER DEFAULT 0,
            boost_channel_id INTEGER,
            boost_message TEXT,
            boost_image TEXT,
            boost_embed INTEGER DEFAULT 1
        )
    """)

    # --------------------------------------------------------
    # Self roles
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS role_panels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            image_url TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS self_roles (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            emoji TEXT,
            custom_id TEXT NOT NULL UNIQUE,
            PRIMARY KEY (guild_id, role_id)
        )
    """)

    # --------------------------------------------------------
    # Warnings
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    logger.info("SQLite database initialized")


init_database()


# ============================================================
# SQLITE HELPERS
# ============================================================

def ensure_guild_settings(guild_id: int):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT guild_id FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    )

    if not cur.fetchone():
        cur.execute(
            "INSERT INTO guild_settings (guild_id) VALUES (?)",
            (guild_id,)
        )

    conn.commit()
    conn.close()


def get_guild_settings(guild_id: int):
    ensure_guild_settings(guild_id)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    )

    row = cur.fetchone()
    conn.close()

    return row


# ============================================================
# FLASK WEB SERVER
# ============================================================

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify({
        "status": "online",
        "service": "Discord Bot"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


def run_web_server():
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


# Start Flask
Thread(
    target=run_web_server,
    daemon=True
).start()


# ============================================================
# SPOTIFY
# ============================================================

spotify_client = None

if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        spotify_client = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET
            )
        )

        logger.info("Spotify API initialized")

    except Exception as e:
        logger.error("Spotify initialization failed: %s", e)
        spotify_client = None
else:
    logger.warning(
        "Spotify credentials not found. "
        "Spotify URL playback will be unavailable."
    )


# ============================================================
# DISCORD INTENTS
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True


# ============================================================
# BOT
# ============================================================

class MusicBot(commands.Bot):

    async def setup_hook(self):

        logger.info("Loading persistent role panels...")

        await load_persistent_role_views()

        logger.info("Syncing Slash Commands...")

        try:
            synced = await self.tree.sync()

            logger.info(
                "Slash Commands Sync สำเร็จ: %d commands",
                len(synced)
            )

        except Exception as e:
            logger.exception(
                "Slash Commands Sync failed: %s",
                e
            )


bot = MusicBot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# MUSIC DATA
# ============================================================

@dataclass
class Song:
    title: str
    url: str
    webpage_url: str
    duration: int
    thumbnail: Optional[str]
    requester_id: int
    source: str


@dataclass
class GuildMusic:
    queue: list
    current: Optional[Song]
    voice_client: Optional[discord.VoiceClient]
    loop: bool
    volume: float
    lock: asyncio.Lock


music_sessions: dict[int, GuildMusic] = {}


def get_music_session(guild_id: int) -> GuildMusic:

    if guild_id not in music_sessions:
        music_sessions[guild_id] = GuildMusic(
            queue=[],
            current=None,
            voice_client=None,
            loop=False,
            volume=1.0,
            lock=asyncio.Lock()
        )

    return music_sessions[guild_id]


# ============================================================
# YT-DLP CONFIG
# ============================================================

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
    "cachedir": False,
}

YTDL_SEARCH_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
    "cachedir": False,
}


# ============================================================
# HELPERS
# ============================================================

def format_duration(seconds: Optional[int]) -> str:

    if not seconds:
        return "ไม่ทราบ"

    seconds = int(seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


def is_url(text: str) -> bool:
    return bool(
        re.match(
            r"^https?://",
            text,
            re.IGNORECASE
        )
    )


def is_spotify_url(text: str) -> bool:

    parsed = urlparse(text)

    return (
        parsed.netloc.lower() in {
            "open.spotify.com",
            "spotify.com"
        }
        and "/track/" in parsed.path
    )


def extract_spotify_track_id(url: str) -> Optional[str]:

    match = re.search(
        r"/track/([A-Za-z0-9]+)",
        url
    )

    if match:
        return match.group(1)

    return None


def replace_variables(
    text: str,
    member: discord.Member
) -> str:

    guild = member.guild

    replacements = {
        "{user}": member.mention,
        "{username}": member.name,
        "{server}": guild.name,
        "{member_count}": str(guild.member_count or 0),
    }

    for key, value in replacements.items():
        text = text.replace(key, value)

    return text


def safe_text(text: Optional[str]) -> str:
    return text if text else ""


# ============================================================
# SPOTIFY -> YOUTUBE
# ============================================================

async def spotify_to_youtube(
    spotify_url: str
) -> Optional[str]:

    if not spotify_client:
        raise RuntimeError(
            "ยังไม่ได้ตั้งค่า SPOTIFY_CLIENT_ID / "
            "SPOTIFY_CLIENT_SECRET"
        )

    track_id = extract_spotify_track_id(spotify_url)

    if not track_id:
        raise ValueError("Spotify URL ไม่ใช่ Track URL ที่ถูกต้อง")

    def fetch_spotify():

        return spotify_client.track(track_id)

    track = await asyncio.to_thread(
        fetch_spotify
    )

    if not track:
        return None

    title = track.get("name")

    artists = track.get("artists", [])

    artist_names = ", ".join(
        artist.get("name", "")
        for artist in artists
    )

    if not title:
        return None

    search_query = f"{title} {artist_names}"

    return search_query


# ============================================================
# YOUTUBE SEARCH / EXTRACTION
# ============================================================

async def extract_youtube_song(
    query: str,
    requester_id: int,
    source_name: str = "YouTube"
) -> Song:

    def extract():

        with yt_dlp.YoutubeDL(
            YTDL_SEARCH_OPTIONS
        ) as ydl:

            if is_url(query):

                info = ydl.extract_info(
                    query,
                    download=False
                )

            else:

                info = ydl.extract_info(
                    f"ytsearch1:{query}",
                    download=False
                )

            if not info:
                raise RuntimeError(
                    "ไม่พบข้อมูลเพลง"
                )

            if "entries" in info:

                entries = info.get("entries")

                if not entries:
                    raise RuntimeError(
                        "ไม่พบเพลงจากการค้นหา"
                    )

                info = entries[0]

            if not info:
                raise RuntimeError(
                    "ไม่พบข้อมูลเพลง"
                )

            stream_url = info.get("url")

            if not stream_url:

                formats = info.get(
                    "formats",
                    []
                )

                audio_formats = [
                    f for f in formats
                    if f.get("acodec") != "none"
                    and f.get("url")
                ]

                if audio_formats:

                    audio_formats.sort(
                        key=lambda x: (
                            x.get("abr") or 0
                        ),
                        reverse=True
                    )

                    stream_url = audio_formats[0].get(
                        "url"
                    )

            if not stream_url:
                raise RuntimeError(
                    "ไม่สามารถหา Audio Stream ของเพลงได้"
                )

            return Song(
                title=info.get(
                    "title",
                    "Unknown"
                ),
                url=stream_url,
                webpage_url=info.get(
                    "webpage_url",
                    query
                ),
                duration=info.get(
                    "duration",
                    0
                ) or 0,
                thumbnail=info.get(
                    "thumbnail"
                ),
                requester_id=requester_id,
                source=source_name
            )

    return await asyncio.to_thread(extract)


async def resolve_song(
    query: str,
    requester_id: int
) -> Song:

    query = query.strip()

    if not query:
        raise ValueError(
            "กรุณาระบุชื่อเพลงหรือ URL"
        )

    # Spotify
    if is_spotify_url(query):

        spotify_query = await spotify_to_youtube(
            query
        )

        if not spotify_query:
            raise RuntimeError(
                "ไม่สามารถอ่านข้อมูลเพลงจาก Spotify ได้"
            )

        return await extract_youtube_song(
            spotify_query,
            requester_id,
            "Spotify → YouTube"
        )

    # YouTube URL
    if is_url(query):

        if (
            "youtube.com" in query.lower()
            or "youtu.be" in query.lower()
        ):

            return await extract_youtube_song(
                query,
                requester_id,
                "YouTube"
            )

        raise ValueError(
            "รองรับ URL ของ YouTube หรือ Spotify เท่านั้น"
        )

    # Search
    return await extract_youtube_song(
        query,
        requester_id,
        "YouTube Search"
    )


# ============================================================
# VOICE CONNECTION
# ============================================================

async def ensure_voice(
    interaction: discord.Interaction
) -> discord.VoiceClient:

    if not interaction.guild:
        raise RuntimeError(
            "คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์"
        )

    member = interaction.guild.get_member(
        interaction.user.id
    )

    if not member:
        raise RuntimeError(
            "ไม่พบข้อมูลสมาชิกของคุณในเซิร์ฟเวอร์"
        )

    if not member.voice or not member.voice.channel:
        raise RuntimeError(
            "คุณต้องอยู่ใน Voice Channel ก่อน"
        )

    target_channel = member.voice.channel

    session = get_music_session(
        interaction.guild.id
    )

    voice_client = interaction.guild.voice_client

    try:

        if voice_client:

            if voice_client.channel.id != target_channel.id:

                await voice_client.move_to(
                    target_channel
                )

            session.voice_client = voice_client

            return voice_client

        voice_client = await target_channel.connect(
            self_deaf=True
        )

        session.voice_client = voice_client

        return voice_client

    except discord.ClientException as e:

        raise RuntimeError(
            f"ไม่สามารถเข้า Voice Channel ได้: {e}"
        )


# ============================================================
# PLAYBACK
# ============================================================

async def play_next(guild: discord.Guild):

    session = get_music_session(
        guild.id
    )

    async with session.lock:

        voice_client = guild.voice_client

        if not voice_client:
            session.current = None
            session.voice_client = None
            return

        if not voice_client.is_connected():
            session.current = None
            session.voice_client = None
            return

        # Loop current song
        if session.loop and session.current:

            song = session.current

        else:

            if not session.queue:

                session.current = None

                if voice_client.is_playing():
                    voice_client.stop()

                return

            song = session.queue.pop(0)

            session.current = song

        try:

            # Refresh stream URL before playing.
            refreshed = await extract_youtube_song(
                song.webpage_url,
                song.requester_id,
                song.source
            )

            song.url = refreshed.url

        except Exception as e:

            logger.error(
                "Failed to refresh audio stream: %s",
                e
            )

            session.current = None

            await play_next(guild)

            return

        ffmpeg_options = {
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5"
            ),
            "options": (
                "-vn "
                "-loglevel warning"
            )
        }

        try:

            source = discord.FFmpegPCMAudio(
                song.url,
                executable=FFMPEG_PATH,
                **ffmpeg_options
            )

            volume_source = discord.PCMVolumeTransformer(
                source,
                volume=session.volume
            )

            def after_play(error):

                if error:
                    logger.error(
                        "Audio playback error: %s",
                        error
                    )

                future = asyncio.run_coroutine_threadsafe(
                    playback_finished(guild),
                    bot.loop
                )

                try:
                    future.result()
                except Exception as callback_error:
                    logger.error(
                        "Playback callback error: %s",
                        callback_error
                    )

            voice_client.play(
                volume_source,
                after=after_play
            )

            logger.info(
                "Playing in %s: %s",
                guild.name,
                song.title
            )

        except Exception as e:

            logger.exception(
                "FFmpeg playback failed: %s",
                e
            )

            session.current = None

            if session.queue:
                await play_next(guild)


async def playback_finished(
    guild: discord.Guild
):

    await asyncio.sleep(0.5)

    session = get_music_session(
        guild.id
    )

    if not guild.voice_client:
        session.current = None
        return

    if session.loop:
        await play_next(guild)
        return

    if session.queue:

        await play_next(guild)

    else:

        session.current = None

        logger.info(
            "Queue finished in %s",
            guild.name
        )


# ============================================================
# EMBEDS
# ============================================================

def song_embed(
    song: Song,
    title: str = "🎵 เพลง"
) -> discord.Embed:

    embed = discord.Embed(
        title=title,
        description=f"**[{song.title}]({song.webpage_url})**",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="⏱️ ระยะเวลา",
        value=format_duration(song.duration),
        inline=True
    )

    embed.add_field(
        name="👤 ขอโดย",
        value=f"<@{song.requester_id}>",
        inline=True
    )

    embed.add_field(
        name="🔊 แหล่งเพลง",
        value=song.source,
        inline=True
    )

    if song.thumbnail:
        embed.set_thumbnail(
            url=song.thumbnail
        )

    return embed


# ============================================================
# /play
# ============================================================

@bot.tree.command(
    name="play",
    description="เล่นเพลงจาก YouTube, Spotify หรือชื่อเพลง"
)
@app_commands.guild_only()
@app_commands.describe(
    query="ลิงก์ YouTube/Spotify หรือชื่อเพลง"
)
async def play(
    interaction: discord.Interaction,
    query: str
):

    await interaction.response.defer()

    if not interaction.guild:
        await interaction.followup.send(
            "❌ คำสั่งนี้ใช้ในเซิร์ฟเวอร์เท่านั้น"
        )
        return

    try:

        voice_client = await ensure_voice(
            interaction
        )

    except Exception as e:

        await interaction.followup.send(
            f"❌ {e}"
        )
        return

    try:

        song = await resolve_song(
            query,
            interaction.user.id
        )

    except yt_dlp.DownloadError as e:

        logger.error(
            "yt-dlp error: %s",
            e
        )

        await interaction.followup.send(
            "❌ ไม่สามารถโหลดเพลงจาก YouTube ได้"
        )

        return

    except Exception as e:

        logger.error(
            "Song resolve error: %s",
            e
        )

        await interaction.followup.send(
            f"❌ ไม่พบเพลงหรือไม่สามารถโหลดเพลงได้\n`{e}`"
        )

        return

    session = get_music_session(
        interaction.guild.id
    )

    async with session.lock:

        if voice_client.is_playing() or voice_client.is_paused():

            session.queue.append(song)

            position = len(session.queue)

            await interaction.followup.send(
                embed=song_embed(
                    song,
                    "➕ เพิ่มเพลงเข้าคิว"
                )
            )

            await interaction.followup.send(
                f"📋 ตำแหน่งในคิว: **#{position}**"
            )

            return

        session.current = song

        # Put current back temporarily so play_next
        # can use the same playback logic.
        if voice_client.is_playing():
            voice_client.stop()

    # Directly play the current song
    try:

        ffmpeg_options = {
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5"
            ),
            "options": "-vn -loglevel warning"
        }

        source = discord.FFmpegPCMAudio(
            song.url,
            executable=FFMPEG_PATH,
            **ffmpeg_options
        )

        volume_source = discord.PCMVolumeTransformer(
            source,
            volume=session.volume
        )

        def after_play(error):

            if error:
                logger.error(
                    "Playback error: %s",
                    error
                )

            future = asyncio.run_coroutine_threadsafe(
                playback_finished(
                    interaction.guild
                ),
                bot.loop
            )

            try:
                future.result()
            except Exception:
                pass

        voice_client.play(
            volume_source,
            after=after_play
        )

        await interaction.followup.send(
            embed=song_embed(
                song,
                "▶️ กำลังเล่น"
            )
        )

    except Exception as e:

        logger.exception(
            "FFmpeg error: %s",
            e
        )

        session.current = None

        await interaction.followup.send(
            "❌ FFmpeg ไม่สามารถเล่นเพลงนี้ได้"
        )


# ============================================================
# /queue
# ============================================================

@bot.tree.command(
    name="queue",
    description="ดูเพลงที่กำลังรอเล่นในคิว"
)
@app_commands.guild_only()
async def queue_command(
    interaction: discord.Interaction
):

    session = get_music_session(
        interaction.guild.id
    )

    embed = discord.Embed(
        title="📋 Music Queue",
        color=discord.Color.blurple()
    )

    if session.current:

        embed.add_field(
            name="▶️ กำลังเล่น",
            value=(
                f"**[{session.current.title}]"
                f"({session.current.webpage_url})**"
            ),
            inline=False
        )

    if not session.queue:

        embed.add_field(
            name="Queue",
            value="ไม่มีเพลงรออยู่",
            inline=False
        )

    else:

        lines = []

        for index, song in enumerate(
            session.queue[:20],
            start=1
        ):

            lines.append(
                f"`{index}.` "
                f"[{song.title}]({song.webpage_url})"
            )

        if len(session.queue) > 20:
            lines.append(
                f"\n... และอีก "
                f"{len(session.queue) - 20} เพลง"
            )

        embed.add_field(
            name="เพลงที่รอ",
            value="\n".join(lines),
            inline=False
        )

    embed.set_footer(
        text=f"จำนวนในคิว: {len(session.queue)}"
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# /skip
# ============================================================

@bot.tree.command(
    name="skip",
    description="ข้ามเพลงที่กำลังเล่น"
)
@app_commands.guild_only()
async def skip(
    interaction: discord.Interaction
):

    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message(
            "❌ บอทไม่ได้อยู่ในห้องเสียง"
        )
        return

    if not voice_client.is_playing():
        await interaction.response.send_message(
            "❌ ไม่มีเพลงที่กำลังเล่น"
        )
        return

    voice_client.stop()

    await interaction.response.send_message(
        "⏭️ ข้ามเพลงแล้ว"
    )


# ============================================================
# /pause
# ============================================================

@bot.tree.command(
    name="pause",
    description="หยุดเพลงชั่วคราว"
)
@app_commands.guild_only()
async def pause(
    interaction: discord.Interaction
):

    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_playing():
        await interaction.response.send_message(
            "❌ ไม่มีเพลงที่กำลังเล่น"
        )
        return

    voice_client.pause()

    await interaction.response.send_message(
        "⏸️ หยุดเพลงชั่วคราวแล้ว"
    )


# ============================================================
# /resume
# ============================================================

@bot.tree.command(
    name="resume",
    description="เล่นเพลงต่อจากที่หยุดไว้"
)
@app_commands.guild_only()
async def resume(
    interaction: discord.Interaction
):

    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_paused():
        await interaction.response.send_message(
            "❌ ไม่มีเพลงที่หยุดชั่วคราว"
        )
        return

    voice_client.resume()

    await interaction.response.send_message(
        "▶️ เล่นเพลงต่อแล้ว"
    )


# ============================================================
# /stop
# ============================================================

@bot.tree.command(
    name="stop",
    description="หยุดเพลงและล้างคิว"
)
@app_commands.guild_only()
async def stop(
    interaction: discord.Interaction
):

    session = get_music_session(
        interaction.guild.id
    )

    session.queue.clear()
    session.current = None

    voice_client = interaction.guild.voice_client

    if voice_client and (
        voice_client.is_playing()
        or voice_client.is_paused()
    ):
        voice_client.stop()

    await interaction.response.send_message(
        "⏹️ หยุดเพลงและล้างคิวแล้ว"
    )


# ============================================================
# /leave
# ============================================================

@bot.tree.command(
    name="leave",
    description="ให้บอทออกจากห้องเสียง"
)
@app_commands.guild_only()
async def leave(
    interaction: discord.Interaction
):

    session = get_music_session(
        interaction.guild.id
    )

    session.queue.clear()
    session.current = None

    voice_client = interaction.guild.voice_client

    if not voice_client:
        await interaction.response.send_message(
            "❌ บอทไม่ได้อยู่ในห้องเสียง"
        )
        return

    await voice_client.disconnect()

    session.voice_client = None

    await interaction.response.send_message(
        "👋 ออกจากห้องเสียงแล้ว"
    )


# ============================================================
# /nowplaying
# ============================================================

@bot.tree.command(
    name="nowplaying",
    description="แสดงเพลงที่กำลังเล่น"
)
@app_commands.guild_only()
async def nowplaying(
    interaction: discord.Interaction
):

    session = get_music_session(
        interaction.guild.id
    )

    if not session.current:

        await interaction.response.send_message(
            "❌ ไม่มีเพลงที่กำลังเล่น"
        )
        return

    embed = song_embed(
        session.current,
        "🎧 Now Playing"
    )

    embed.add_field(
        name="🔁 Loop",
        value="เปิด" if session.loop else "ปิด",
        inline=True
    )

    embed.add_field(
        name="🔊 Volume",
        value=f"{int(session.volume * 100)}%",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# /loop
# ============================================================

@bot.tree.command(
    name="loop",
    description="เปิดหรือปิดการวนเพลง"
)
@app_commands.guild_only()
async def loop_command(
    interaction: discord.Interaction
):

    session = get_music_session(
        interaction.guild.id
    )

    session.loop = not session.loop

    await interaction.response.send_message(
        f"🔁 Loop: **{'เปิด' if session.loop else 'ปิด'}**"
    )


# ============================================================
# /volume
# ============================================================

@bot.tree.command(
    name="volume",
    description="ปรับระดับเสียงเพลง"
)
@app_commands.guild_only()
@app_commands.describe(
    percent="ระดับเสียง 0-100"
)
async def volume(
    interaction: discord.Interaction,
    percent: app_commands.Range[int, 0, 100]
):

    session = get_music_session(
        interaction.guild.id
    )

    session.volume = percent / 100

    voice_client = interaction.guild.voice_client

    if voice_client and voice_client.source:

        source = voice_client.source

        if isinstance(
            source,
            discord.PCMVolumeTransformer
        ):
            source.volume = session.volume

    await interaction.response.send_message(
        f"🔊 ตั้งเสียงเป็น **{percent}%**"
    )


# ============================================================
# PERMISSION HELPERS
# ============================================================

def has_permission(
    interaction: discord.Interaction,
    permission: str
) -> bool:

    if not interaction.guild:
        return False

    member = interaction.guild.get_member(
        interaction.user.id
    )

    if not member:
        return False

    permissions = member.guild_permissions

    if permissions.administrator:
        return True

    return getattr(
        permissions,
        permission,
        False
    )


def bot_has_permission(
    guild: discord.Guild,
    permission: str
) -> bool:

    me = guild.me

    if not me:
        return False

    if me.guild_permissions.administrator:
        return True

    return getattr(
        me.guild_permissions,
        permission,
        False
    )


async def permission_error(
    interaction: discord.Interaction,
    permission_name: str
):

    message = (
        f"❌ คุณไม่มี Permission สำหรับคำสั่งนี้\n"
        f"ต้องมี: **{permission_name}**"
    )

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True
        )


# ============================================================
# ROLE HIERARCHY
# ============================================================

def bot_can_manage_role(
    guild: discord.Guild,
    role: discord.Role
) -> bool:

    me = guild.me

    if not me:
        return False

    if role.is_default():
        return False

    return role < me.top_role


def member_target_valid(
    guild: discord.Guild,
    target: discord.Member
) -> bool:

    me = guild.me

    if not me:
        return False

    return target != me and target.top_role < me.top_role


# ============================================================
# /setup_roles
# ============================================================

@bot.tree.command(
    name="setup_roles",
    description="สร้างแผงปุ่มสำหรับรับยศ"
)
@app_commands.guild_only()
@app_commands.describe(
    channel="ห้องที่จะส่งแผงรับยศ",
    title="ชื่อแผงรับยศ",
    description="คำอธิบายแผงรับยศ",
    image_url="URL รูปภาพหรือ GIF"
)
async def setup_roles(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    image_url: Optional[str] = None
):

    if not has_permission(
        interaction,
        "manage_roles"
    ):

        await permission_error(
            interaction,
            "Manage Roles"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "manage_roles"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Manage Roles`",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=title[:256],
        description=description[:4096],
        color=discord.Color.blurple()
    )

    if image_url:
        embed.set_image(
            url=image_url
        )

    view = SelfRoleView(
        interaction.guild.id
    )

    try:

        message = await channel.send(
            embed=embed,
            view=view
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ส่งข้อความในห้องนี้",
            ephemeral=True
        )
        return

    except discord.HTTPException:

        await interaction.response.send_message(
            "❌ Discord API ไม่สามารถส่งแผงรับยศได้",
            ephemeral=True
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO role_panels
        (
            guild_id,
            channel_id,
            message_id,
            title,
            description,
            image_url
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            interaction.guild.id,
            channel.id,
            message.id,
            title,
            description,
            image_url
        )
    )

    conn.commit()
    conn.close()

    bot.add_view(
        view,
        message_id=message.id
    )

    await interaction.response.send_message(
        f"✅ สร้างแผงรับยศที่ {channel.mention} แล้ว",
        ephemeral=True
    )


# ============================================================
# SELF ROLE VIEW
# ============================================================

class RoleButton(
    discord.ui.Button
):

    def __init__(
        self,
        guild_id: int,
        role_id: int,
        label: str,
        emoji: Optional[str],
        custom_id: str
    ):

        super().__init__(
            label=label[:80],
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            custom_id=custom_id
        )

        self.guild_id = guild_id
        self.role_id = role_id

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        guild = interaction.guild

        if not guild:

            await interaction.response.send_message(
                "❌ คำสั่งนี้ใช้ในเซิร์ฟเวอร์เท่านั้น",
                ephemeral=True
            )
            return

        role = guild.get_role(
            self.role_id
        )

        if not role:

            await interaction.response.send_message(
                "❌ ไม่พบยศนี้แล้ว",
                ephemeral=True
            )
            return

        if not bot_has_permission(
            guild,
            "manage_roles"
        ):

            await interaction.response.send_message(
                "❌ บอทไม่มี Permission `Manage Roles`",
                ephemeral=True
            )
            return

        if not bot_can_manage_role(
            guild,
            role
        ):

            await interaction.response.send_message(
                "❌ บอทไม่สามารถจัดการยศนี้ได้ "
                "เพราะยศอยู่สูงกว่าหรือเท่ากับยศสูงสุดของบอท",
                ephemeral=True
            )
            return

        member = guild.get_member(
            interaction.user.id
        )

        if not member:

            await interaction.response.send_message(
                "❌ ไม่พบสมาชิก",
                ephemeral=True
            )
            return

        try:

            if role in member.roles:

                await member.remove_roles(
                    role,
                    reason="Self Role Button"
                )

                await interaction.response.send_message(
                    f"➖ ถอดยศ {role.mention} แล้ว",
                    ephemeral=True
                )

            else:

                await member.add_roles(
                    role,
                    reason="Self Role Button"
                )

                await interaction.response.send_message(
                    f"✅ รับยศ {role.mention} แล้ว",
                    ephemeral=True
                )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ บอทไม่มีสิทธิ์จัดการยศนี้",
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "❌ Discord API เกิดข้อผิดพลาด",
                ephemeral=True
            )


class SelfRoleView(
    discord.ui.View
):

    def __init__(
        self,
        guild_id: int
    ):

        super().__init__(
            timeout=None
        )

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT role_id, label, emoji, custom_id
            FROM self_roles
            WHERE guild_id = ?
            ORDER BY role_id
            LIMIT 25
            """,
            (guild_id,)
        )

        rows = cur.fetchall()
        conn.close()

        for row in rows:

            self.add_item(
                RoleButton(
                    guild_id=guild_id,
                    role_id=row["role_id"],
                    label=row["label"],
                    emoji=row["emoji"],
                    custom_id=row["custom_id"]
                )
            )


# ============================================================
# LOAD PERSISTENT VIEWS
# ============================================================

async def load_persistent_role_views():

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT guild_id, message_id
        FROM role_panels
        """
    )

    panels = cur.fetchall()
    conn.close()

    for panel in panels:

        try:

            view = SelfRoleView(
                panel["guild_id"]
            )

            bot.add_view(
                view,
                message_id=panel["message_id"]
            )

        except Exception as e:

            logger.error(
                "Failed loading role panel %s: %s",
                panel["message_id"],
                e
            )


# ============================================================
# UPDATE ROLE PANEL
# ============================================================

async def update_role_panel(
    guild: discord.Guild
):

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM role_panels
        WHERE guild_id = ?
        """,
        (guild.id,)
    )

    panel = cur.fetchone()

    conn.close()

    if not panel:
        return

    channel = guild.get_channel(
        panel["channel_id"]
    )

    if not isinstance(
        channel,
        discord.TextChannel
    ):
        return

    try:

        message = await channel.fetch_message(
            panel["message_id"]
        )

    except discord.HTTPException:
        return

    embed = discord.Embed(
        title=panel["title"],
        description=panel["description"] or "",
        color=discord.Color.blurple()
    )

    if panel["image_url"]:
        embed.set_image(
            url=panel["image_url"]
        )

    view = SelfRoleView(
        guild.id
    )

    try:

        await message.edit(
            embed=embed,
            view=view
        )

    except discord.HTTPException as e:

        logger.error(
            "Failed updating role panel: %s",
            e
        )


# ============================================================
# /add_role
# ============================================================

@bot.tree.command(
    name="add_role",
    description="เพิ่มยศเข้าแผงรับยศ"
)
@app_commands.guild_only()
@app_commands.describe(
    role="ยศที่ต้องการเพิ่ม",
    label="ชื่อบนปุ่ม",
    emoji="Emoji ของปุ่ม"
)
async def add_role(
    interaction: discord.Interaction,
    role: discord.Role,
    label: Optional[str] = None,
    emoji: Optional[str] = None
):

    if not has_permission(
        interaction,
        "manage_roles"
    ):

        await permission_error(
            interaction,
            "Manage Roles"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "manage_roles"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Manage Roles`",
            ephemeral=True
        )
        return

    if not bot_can_manage_role(
        interaction.guild,
        role
    ):

        await interaction.response.send_message(
            "❌ ยศนี้อยู่สูงกว่าหรือเท่ากับยศสูงสุดของบอท",
            ephemeral=True
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*)
        FROM self_roles
        WHERE guild_id = ?
        """,
        (interaction.guild.id,)
    )

    count = cur.fetchone()[0]

    if count >= 25:

        conn.close()

        await interaction.response.send_message(
            "❌ แผงหนึ่งสามารถมีปุ่มได้สูงสุด 25 ปุ่ม",
            ephemeral=True
        )
        return

    custom_id = (
        f"selfrole:{interaction.guild.id}:{role.id}"
    )

    cur.execute(
        """
        INSERT OR REPLACE INTO self_roles
        (
            guild_id,
            role_id,
            label,
            emoji,
            custom_id
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            interaction.guild.id,
            role.id,
            (label or role.name)[:80],
            emoji,
            custom_id
        )
    )

    conn.commit()
    conn.close()

    await update_role_panel(
        interaction.guild
    )

    await interaction.response.send_message(
        f"✅ เพิ่ม {role.mention} เข้าแผงรับยศแล้ว",
        ephemeral=True
    )


# ============================================================
# /remove_role
# ============================================================

@bot.tree.command(
    name="remove_role",
    description="ลบยศออกจากแผงรับยศ"
)
@app_commands.guild_only()
@app_commands.describe(
    role="ยศที่ต้องการลบออกจากระบบ"
)
async def remove_role(
    interaction: discord.Interaction,
    role: discord.Role
):

    if not has_permission(
        interaction,
        "manage_roles"
    ):

        await permission_error(
            interaction,
            "Manage Roles"
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM self_roles
        WHERE guild_id = ?
        AND role_id = ?
        """,
        (
            interaction.guild.id,
            role.id
        )
    )

    deleted = cur.rowcount

    conn.commit()
    conn.close()

    if not deleted:

        await interaction.response.send_message(
            "❌ ยศนี้ไม่ได้อยู่ในระบบรับยศ",
            ephemeral=True
        )
        return

    await update_role_panel(
        interaction.guild
    )

    await interaction.response.send_message(
        f"✅ ลบ {role.mention} ออกจากแผงรับยศแล้ว",
        ephemeral=True
    )


# ============================================================
# /ban
# ============================================================

@bot.tree.command(
    name="ban",
    description="แบนสมาชิกออกจากเซิร์ฟเวอร์"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการแบน",
    reason="เหตุผลในการแบน"
)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    if not has_permission(
        interaction,
        "ban_members"
    ):

        await permission_error(
            interaction,
            "Ban Members"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "ban_members"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Ban Members`",
            ephemeral=True
        )
        return

    if not member_target_valid(
        interaction.guild,
        member
    ):

        await interaction.response.send_message(
            "❌ ไม่สามารถแบนสมาชิกที่มียศสูงกว่าหรือเท่ากับบอทได้",
            ephemeral=True
        )
        return

    try:

        await member.ban(
            reason=reason
        )

        await interaction.response.send_message(
            f"🔨 แบน {member.mention} แล้ว\n"
            f"เหตุผล: {reason}"
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์แบนสมาชิกนี้",
            ephemeral=True
        )


# ============================================================
# /kick
# ============================================================

@bot.tree.command(
    name="kick",
    description="เตะสมาชิกออกจากเซิร์ฟเวอร์"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการเตะ",
    reason="เหตุผลในการเตะ"
)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    if not has_permission(
        interaction,
        "kick_members"
    ):

        await permission_error(
            interaction,
            "Kick Members"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "kick_members"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Kick Members`",
            ephemeral=True
        )
        return

    if not member_target_valid(
        interaction.guild,
        member
    ):

        await interaction.response.send_message(
            "❌ ไม่สามารถเตะสมาชิกที่มียศสูงกว่าหรือเท่ากับบอทได้",
            ephemeral=True
        )
        return

    try:

        await member.kick(
            reason=reason
        )

        await interaction.response.send_message(
            f"👢 เตะ {member.mention} แล้ว\n"
            f"เหตุผล: {reason}"
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์เตะสมาชิกนี้",
            ephemeral=True
        )


# ============================================================
# /timeout
# ============================================================

@bot.tree.command(
    name="timeout",
    description="Timeout สมาชิกชั่วคราว"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการ Timeout",
    minutes="จำนวนนาที",
    reason="เหตุผล"
)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    if not has_permission(
        interaction,
        "moderate_members"
    ):

        await permission_error(
            interaction,
            "Moderate Members"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "moderate_members"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Moderate Members`",
            ephemeral=True
        )
        return

    if not member_target_valid(
        interaction.guild,
        member
    ):

        await interaction.response.send_message(
            "❌ ไม่สามารถ Timeout สมาชิกที่มียศสูงกว่าหรือเท่ากับบอทได้",
            ephemeral=True
        )
        return

    try:

        until = discord.utils.utcnow() + timedelta(
            minutes=minutes
        )

        await member.timeout(
            until,
            reason=reason
        )

        await interaction.response.send_message(
            f"⏱️ Timeout {member.mention} "
            f"เป็นเวลา **{minutes} นาที** แล้ว"
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ Timeout สมาชิกนี้",
            ephemeral=True
        )


# ============================================================
# /untimeout
# ============================================================

@bot.tree.command(
    name="untimeout",
    description="ยกเลิก Timeout สมาชิก"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการยกเลิก Timeout"
)
async def untimeout(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not has_permission(
        interaction,
        "moderate_members"
    ):

        await permission_error(
            interaction,
            "Moderate Members"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "moderate_members"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Moderate Members`",
            ephemeral=True
        )
        return

    try:

        await member.timeout(
            None,
            reason=f"Untimeout by {interaction.user}"
        )

        await interaction.response.send_message(
            f"✅ ยกเลิก Timeout {member.mention} แล้ว"
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ยกเลิก Timeout สมาชิกนี้",
            ephemeral=True
        )


# ============================================================
# /warn
# ============================================================

@bot.tree.command(
    name="warn",
    description="เตือนสมาชิก"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการเตือน",
    reason="เหตุผล"
)
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    if not has_permission(
        interaction,
        "moderate_members"
    ):

        await permission_error(
            interaction,
            "Moderate Members"
        )
        return

    if not member_target_valid(
        interaction.guild,
        member
    ):

        await interaction.response.send_message(
            "❌ ไม่สามารถ Warn สมาชิกที่มียศสูงกว่าหรือเท่ากับบอทได้",
            ephemeral=True
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO warnings
        (
            guild_id,
            user_id,
            moderator_id,
            reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            interaction.guild.id,
            member.id,
            interaction.user.id,
            reason,
            datetime.now(UTC).isoformat()
        )
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"⚠️ Warn {member.mention} แล้ว\n"
        f"เหตุผล: {reason}"
    )


# ============================================================
# /warnings
# ============================================================

@bot.tree.command(
    name="warnings",
    description="ดูประวัติคำเตือนของสมาชิก"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการดูคำเตือน"
)
async def warnings(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not has_permission(
        interaction,
        "moderate_members"
    ):

        await permission_error(
            interaction,
            "Moderate Members"
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM warnings
        WHERE guild_id = ?
        AND user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (
            interaction.guild.id,
            member.id
        )
    )

    rows = cur.fetchall()
    conn.close()

    embed = discord.Embed(
        title=f"⚠️ Warnings - {member}",
        color=discord.Color.orange()
    )

    if not rows:

        embed.description = "ไม่มีประวัติคำเตือน"

    else:

        lines = []

        for row in rows:

            moderator = interaction.guild.get_member(
                row["moderator_id"]
            )

            moderator_text = (
                moderator.mention
                if moderator
                else f"<@{row['moderator_id']}>"
            )

            lines.append(
                f"**#{row['id']}** — {row['reason']}\n"
                f"โดย {moderator_text}"
            )

        embed.description = "\n\n".join(
            lines
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /clearwarn
# ============================================================

@bot.tree.command(
    name="clearwarn",
    description="ล้างคำเตือนของสมาชิกทั้งหมด"
)
@app_commands.guild_only()
@app_commands.describe(
    member="สมาชิกที่ต้องการล้างคำเตือน"
)
async def clearwarn(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not has_permission(
        interaction,
        "moderate_members"
    ):

        await permission_error(
            interaction,
            "Moderate Members"
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM warnings
        WHERE guild_id = ?
        AND user_id = ?
        """,
        (
            interaction.guild.id,
            member.id
        )
    )

    deleted = cur.rowcount

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🧹 ล้างคำเตือน {member.mention} แล้ว "
        f"จำนวน **{deleted}** รายการ"
    )


# ============================================================
# /purge
# ============================================================

@bot.tree.command(
    name="purge",
    description="ลบข้อความย้อนหลังตามจำนวนที่กำหนด"
)
@app_commands.guild_only()
@app_commands.describe(
    amount="จำนวนข้อความที่ต้องการลบ 1-100"
)
async def purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100]
):

    if not has_permission(
        interaction,
        "manage_messages"
    ):

        await permission_error(
            interaction,
            "Manage Messages"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "manage_messages"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Manage Messages`",
            ephemeral=True
        )
        return

    if not isinstance(
        interaction.channel,
        discord.TextChannel
    ):

        await interaction.response.send_message(
            "❌ คำสั่งนี้ใช้ใน Text Channel เท่านั้น",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        deleted = await interaction.channel.purge(
            limit=amount
        )

        await interaction.followup.send(
            f"🧹 ลบข้อความแล้ว **{len(deleted)}** ข้อความ",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ลบข้อความ",
            ephemeral=True
        )


# ============================================================
# /unban
# ============================================================

@bot.tree.command(
    name="unban",
    description="ปลดแบนสมาชิกด้วย User ID"
)
@app_commands.guild_only()
@app_commands.describe(
    user_id="Discord User ID ของคนที่ต้องการปลดแบน"
)
async def unban(
    interaction: discord.Interaction,
    user_id: str
):

    if not has_permission(
        interaction,
        "ban_members"
    ):

        await permission_error(
            interaction,
            "Ban Members"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "ban_members"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Ban Members`",
            ephemeral=True
        )
        return

    try:

        user = await bot.fetch_user(
            int(user_id)
        )

    except (ValueError, discord.NotFound):

        await interaction.response.send_message(
            "❌ User ID ไม่ถูกต้องหรือไม่พบผู้ใช้",
            ephemeral=True
        )
        return

    try:

        await interaction.guild.unban(
            user
        )

        await interaction.response.send_message(
            f"✅ ปลดแบน {user.mention} แล้ว"
        )

    except discord.NotFound:

        await interaction.response.send_message(
            "❌ ผู้ใช้นี้ไม่ได้ถูกแบน",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ปลดแบน",
            ephemeral=True
        )


# ============================================================
# /announce
# ============================================================

@bot.tree.command(
    name="announce",
    description="ส่งประกาศไปยังห้องที่กำหนด"
)
@app_commands.guild_only()
@app_commands.describe(
    channel="ห้องที่จะส่งประกาศ",
    message="ข้อความประกาศ",
    embed="ให้ส่งเป็น Embed หรือไม่",
    image_url="URL รูปภาพหรือ GIF"
)
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
    embed: bool = True,
    image_url: Optional[str] = None
):

    if not has_permission(
        interaction,
        "manage_guild"
    ):

        await permission_error(
            interaction,
            "Manage Server"
        )
        return

    if not bot_has_permission(
        interaction.guild,
        "send_messages"
    ):

        await interaction.response.send_message(
            "❌ บอทไม่มี Permission `Send Messages`",
            ephemeral=True
        )
        return

    try:

        if embed:

            announcement = discord.Embed(
                description=message[:4096],
                color=discord.Color.blurple()
            )

            if image_url:
                announcement.set_image(
                    url=image_url
                )

            await channel.send(
                embed=announcement
            )

        else:

            content = message

            if image_url:
                content += f"\n{image_url}"

            await channel.send(
                content=content
            )

        await interaction.response.send_message(
            f"📢 ส่งประกาศไปที่ {channel.mention} แล้ว",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ส่งข้อความในห้องนั้น",
            ephemeral=True
        )


# ============================================================
# MEMBER SYSTEM CONFIG BUILDER
# ============================================================

def build_member_embed(
    title: str,
    message: str,
    image_url: Optional[str],
    member: discord.Member
) -> discord.Embed:

    text = replace_variables(
        message,
        member
    )

    embed = discord.Embed(
        title=title,
        description=text[:4096],
        color=discord.Color.blurple()
    )

    if image_url:
        embed.set_image(
            url=image_url
        )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    return embed


# ============================================================
# /setwelcome
# ============================================================

@bot.tree.command(
    name="setwelcome",
    description="ตั้งค่าระบบต้อนรับสมาชิกใหม่"
)
@app_commands.guild_only()
@app_commands.describe(
    channel="ห้องที่จะส่ง Welcome",
    message="ข้อความ เช่น ยินดีต้อนรับ {user} สู่ {server}",
    image_url="URL รูปภาพหรือ GIF",
    embed="ส่งเป็น Embed หรือไม่",
    enabled="เปิดหรือปิดระบบ"
)
async def setwelcome(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    message: Optional[str] = None,
    image_url: Optional[str] = None,
    embed: bool = True,
    enabled: Optional[bool] = None
):

    if not has_permission(
        interaction,
        "manage_guild"
    ):

        await permission_error(
            interaction,
            "Manage Server"
        )
        return

    ensure_guild_settings(
        interaction.guild.id
    )

    current = get_guild_settings(
        interaction.guild.id
    )

    new_channel = (
        channel.id
        if channel
        else current["welcome_channel_id"]
    )

    new_message = (
        message
        if message is not None
        else current["welcome_message"]
    )

    new_image = (
        image_url
        if image_url is not None
        else current["welcome_image"]
    )

    new_enabled = (
        int(enabled)
        if enabled is not None
        else current["welcome_enabled"]
    )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE guild_settings
        SET
            welcome_enabled = ?,
            welcome_channel_id = ?,
            welcome_message = ?,
            welcome_image = ?,
            welcome_embed = ?
        WHERE guild_id = ?
        """,
        (
            new_enabled,
            new_channel,
            new_message,
            new_image,
            int(embed),
            interaction.guild.id
        )
    )

    conn.commit()
    conn.close()

    status = "เปิด" if new_enabled else "ปิด"

    await interaction.response.send_message(
        f"👋 Welcome: **{status}**\n"
        f"ห้อง: "
        f"{f'<#{new_channel}>' if new_channel else 'ยังไม่ได้ตั้ง'}",
        ephemeral=True
    )


# ============================================================
# /setgoodbye
# ============================================================

@bot.tree.command(
    name="setgoodbye",
    description="ตั้งค่าระบบแจ้งเตือนสมาชิกออก"
)
@app_commands.guild_only()
@app_commands.describe(
    channel="ห้องที่จะส่ง Goodbye",
    message="ข้อความ เช่น ลาก่อน {user} จาก {server}",
    image_url="URL รูปภาพหรือ GIF",
    embed="ส่งเป็น Embed หรือไม่",
    enabled="เปิดหรือปิดระบบ"
)
async def setgoodbye(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    message: Optional[str] = None,
    image_url: Optional[str] = None,
    embed: bool = True,
    enabled: Optional[bool] = None
):

    if not has_permission(
        interaction,
        "manage_guild"
    ):

        await permission_error(
            interaction,
            "Manage Server"
        )
        return

    ensure_guild_settings(
        interaction.guild.id
    )

    current = get_guild_settings(
        interaction.guild.id
    )

    new_channel = (
        channel.id
        if channel
        else current["goodbye_channel_id"]
    )

    new_message = (
        message
        if message is not None
        else current["goodbye_message"]
    )

    new_image = (
        image_url
        if image_url is not None
        else current["goodbye_image"]
    )

    new_enabled = (
        int(enabled)
        if enabled is not None
        else current["goodbye_enabled"]
    )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE guild_settings
        SET
            goodbye_enabled = ?,
            goodbye_channel_id = ?,
            goodbye_message = ?,
            goodbye_image = ?,
            goodbye_embed = ?
        WHERE guild_id = ?
        """,
        (
            new_enabled,
            new_channel,
            new_message,
            new_image,
            int(embed),
            interaction.guild.id
        )
    )

    conn.commit()
    conn.close()

    status = "เปิด" if new_enabled else "ปิด"

    await interaction.response.send_message(
        f"👋 Goodbye: **{status}**\n"
        f"ห้อง: "
        f"{f'<#{new_channel}>' if new_channel else 'ยังไม่ได้ตั้ง'}",
        ephemeral=True
    )


# ============================================================
# /setboots
# ============================================================

@bot.tree.command(
    name="setboots",
    description="ตั้งค่าระบบแจ้งเตือนเมื่อมีสมาชิก Boost"
)
@app_commands.guild_only()
@app_commands.describe(
    channel="ห้องที่จะส่งข้อความ Boost",
    message="ข้อความ เช่น ขอบคุณ {user} ที่ Boost {server}",
    image_url="URL รูปภาพหรือ GIF",
    embed="ส่งเป็น Embed หรือไม่",
    enabled="เปิดหรือปิดระบบ"
)
async def setboots(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    message: Optional[str] = None,
    image_url: Optional[str] = None,
    embed: bool = True,
    enabled: Optional[bool] = None
):

    if not has_permission(
        interaction,
        "manage_guild"
    ):

        await permission_error(
            interaction,
            "Manage Server"
        )
        return

    ensure_guild_settings(
        interaction.guild.id
    )

    current = get_guild_settings(
        interaction.guild.id
    )

    new_channel = (
        channel.id
        if channel
        else current["boost_channel_id"]
    )

    new_message = (
        message
        if message is not None
        else current["boost_message"]
    )

    new_image = (
        image_url
        if image_url is not None
        else current["boost_image"]
    )

    new_enabled = (
        int(enabled)
        if enabled is not None
        else current["boost_enabled"]
    )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE guild_settings
        SET
            boost_enabled = ?,
            boost_channel_id = ?,
            boost_message = ?,
            boost_image = ?,
            boost_embed = ?
        WHERE guild_id = ?
        """,
        (
            new_enabled,
            new_channel,
            new_message,
            new_image,
            int(embed),
            interaction.guild.id
        )
    )

    conn.commit()
    conn.close()

    status = "เปิด" if new_enabled else "ปิด"

    await interaction.response.send_message(
        f"🚀 Boost Notification: **{status}**\n"
        f"ห้อง: "
        f"{f'<#{new_channel}>' if new_channel else 'ยังไม่ได้ตั้ง'}",
        ephemeral=True
    )


# ============================================================
# WELCOME EVENT
# ============================================================

@bot.event
async def on_member_join(
    member: discord.Member
):

    try:

        settings = get_guild_settings(
            member.guild.id
        )

        if not settings["welcome_enabled"]:
            return

        channel_id = settings[
            "welcome_channel_id"
        ]

        message = settings[
            "welcome_message"
        ]

        if not channel_id or not message:
            return

        channel = member.guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            return

        if settings["welcome_embed"]:

            embed = build_member_embed(
                "👋 Welcome!",
                message,
                settings["welcome_image"],
                member
            )

            await channel.send(
                embed=embed
            )

        else:

            text = replace_variables(
                message,
                member
            )

            if settings["welcome_image"]:
                text += (
                    f"\n{settings['welcome_image']}"
                )

            await channel.send(
                text
            )

    except Exception as e:

        logger.error(
            "Welcome event error: %s",
            e
        )


# ============================================================
# GOODBYE EVENT
# ============================================================

@bot.event
async def on_member_remove(
    member: discord.Member
):

    try:

        settings = get_guild_settings(
            member.guild.id
        )

        if not settings["goodbye_enabled"]:
            return

        channel_id = settings[
            "goodbye_channel_id"
        ]

        message = settings[
            "goodbye_message"
        ]

        if not channel_id or not message:
            return

        channel = member.guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            return

        if settings["goodbye_embed"]:

            embed = build_member_embed(
                "👋 Goodbye!",
                message,
                settings["goodbye_image"],
                member
            )

            await channel.send(
                embed=embed
            )

        else:

            text = replace_variables(
                message,
                member
            )

            if settings["goodbye_image"]:
                text += (
                    f"\n{settings['goodbye_image']}"
                )

            await channel.send(
                text
            )

    except Exception as e:

        logger.error(
            "Goodbye event error: %s",
            e
        )


# ============================================================
# BOOST EVENT
# ============================================================

@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member
):

    try:

        # Only detect first-time/new boost
        if (
            before.premium_since is not None
            or after.premium_since is None
        ):
            return

        settings = get_guild_settings(
            after.guild.id
        )

        if not settings["boost_enabled"]:
            return

        channel_id = settings[
            "boost_channel_id"
        ]

        message = settings[
            "boost_message"
        ]

        if not channel_id or not message:
            return

        channel = after.guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            return

        if settings["boost_embed"]:

            embed = build_member_embed(
                "🚀 Server Boost!",
                message,
                settings["boost_image"],
                after
            )

            await channel.send(
                embed=embed
            )

        else:

            text = replace_variables(
                message,
                after
            )

            if settings["boost_image"]:
                text += (
                    f"\n{settings['boost_image']}"
                )

            await channel.send(
                text
            )

    except Exception as e:

        logger.error(
            "Boost event error: %s",
            e
        )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    logger.info("=" * 60)

    logger.info(
        "Bot Online: %s (%s)",
        bot.user,
        bot.user.id if bot.user else "unknown"
    )

    logger.info(
        "จำนวนเซิร์ฟเวอร์: %d",
        len(bot.guilds)
    )

    # Activity: Listening to /play
    activity = discord.Activity(
        type=discord.ActivityType.listening,
        name="/play"
    )

    await bot.change_presence(
        status=discord.Status.online,
        activity=activity
    )

    logger.info(
        "Activity: Listening to /play"
    )

    logger.info("=" * 60)


# ============================================================
# GLOBAL SLASH COMMAND ERROR HANDLER
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    logger.error(
        "Slash Command Error: %s",
        error
    )

    # --------------------------------------------------------
    # Permission
    # --------------------------------------------------------

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ คุณไม่มี Permission "
            "สำหรับใช้คำสั่งนี้"
        )

    # --------------------------------------------------------
    # Bot Permission
    # --------------------------------------------------------

    elif isinstance(
        error,
        app_commands.BotMissingPermissions
    ):

        message = (
            "❌ บอทไม่มี Permission "
            "ที่จำเป็นสำหรับคำสั่งนี้"
        )

    # --------------------------------------------------------
    # Check Failure
    # --------------------------------------------------------

    elif isinstance(
        error,
        app_commands.CheckFailure
    ):

        message = (
            "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้"
        )

    # --------------------------------------------------------
    # Command Not Found
    # --------------------------------------------------------

    elif isinstance(
        error,
        app_commands.CommandNotFound
    ):

        message = (
            "❌ ไม่พบ Slash Command นี้"
        )

    # --------------------------------------------------------
    # Discord HTTP
    # --------------------------------------------------------

    elif isinstance(
        error,
        discord.HTTPException
    ):

        message = (
            "❌ Discord API เกิดข้อผิดพลาด "
            "กรุณาลองใหม่อีกครั้ง"
        )

    # --------------------------------------------------------
    # Generic
    # --------------------------------------------------------

    else:

        original = getattr(
            error,
            "original",
            error
        )

        message = (
            "❌ เกิดข้อผิดพลาดที่ไม่คาดคิด\n"
            f"`{type(original).__name__}: "
            f"{original}`"
        )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except Exception as e:

        logger.error(
            "Could not send error response: %s",
            e
        )


# ============================================================
# NORMAL BOT ERROR
# ============================================================

@bot.event
async def on_error(
    event_method,
    *args,
    **kwargs
):

    logger.exception(
        "Unhandled Discord event error: %s",
        event_method
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def close_bot():

    for session in music_sessions.values():

        try:

            if session.voice_client:
                await session.voice_client.disconnect()

        except Exception:
            pass

    await bot.close()


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":

    try:

        bot.run(
            DISCORD_TOKEN,
            log_handler=None
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped by keyboard interrupt"
        )

    except Exception as e:

        logger.exception(
            "Fatal bot error: %s",
            e
        )
