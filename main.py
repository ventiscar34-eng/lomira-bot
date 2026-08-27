import os
import asyncio
import re
from threading import Thread

from flask import Flask

import discord
from discord import app_commands

import yt_dlp
import imageio_ffmpeg

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")


# =========================================================
# FLASK WEB SERVER
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "LOMIRA Bot is Alive!"


@app.route("/health")
def health():
    return "OK"


def run_web():
    port = int(os.getenv("PORT", "8080"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


def keep_alive():
    thread = Thread(
        target=run_web,
        daemon=True
    )
    thread.start()


# =========================================================
# SPOTIFY
# =========================================================

sp = None

if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:

    try:
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET
            )
        )

        print("[SPOTIFY] Connected successfully!")

    except Exception as e:
        print(f"[SPOTIFY ERROR] {repr(e)}")

else:
    print("[SPOTIFY] Credentials not found.")
    print("[SPOTIFY] Spotify links will be searched normally.")


def get_query_from_input(query: str) -> str:

    spotify_pattern = (
        r"(?:https?://)?"
        r"open\.spotify\.com/track/"
        r"([a-zA-Z0-9]+)"
    )

    match = re.search(
        spotify_pattern,
        query
    )

    if match and sp:

        track_id = match.group(1)

        try:

            track = sp.track(track_id)

            track_name = track["name"]

            artists = ", ".join(
                artist["name"]
                for artist in track["artists"]
            )

            result = f"{track_name} {artists}"

            print(
                f"[SPOTIFY] {query} -> {result}"
            )

            return result

        except Exception as e:

            print(
                f"[SPOTIFY ERROR] {repr(e)}"
            )

    return query


# =========================================================
# FFMPEG
# =========================================================

try:

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

    print(
        f"[FFMPEG] Found: {ffmpeg_path}"
    )

except Exception as e:

    ffmpeg_path = None

    print(
        f"[FFMPEG ERROR] {repr(e)}"
    )


# =========================================================
# YT-DLP
# =========================================================

YTDL_OPTIONS = {

    "format": "bestaudio/best",

    "noplaylist": True,

    "skip_download": True,

    "default_search": "ytsearch1",

    "source_address": "0.0.0.0",

    "quiet": True,

    "no_warnings": True,

    "nocheckcertificate": True,

    "socket_timeout": 30,

    "retries": 3,

    "fragment_retries": 3,

    "outtmpl": "%(id)s.%(ext)s",
}


FFMPEG_OPTIONS = {

    "before_options":
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5",

    "options":
        "-vn"
}


# =========================================================
# SONG QUEUE
# =========================================================

song_queues = {}

current_songs = {}


def get_queue(guild_id):

    if guild_id not in song_queues:
        song_queues[guild_id] = []

    return song_queues[guild_id]


def format_duration(seconds):

    if seconds is None:
        return "ไม่ทราบเวลา"

    try:
        seconds = int(seconds)

    except Exception:
        return "ไม่ทราบเวลา"

    hours, remainder = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    if hours > 0:

        return (
            f"{hours}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes}:"
        f"{seconds:02d}"
    )


# =========================================================
# SEARCH SONG
# =========================================================

async def search_song(query):

    loop = asyncio.get_running_loop()

    def extract():

        try:

            options = dict(YTDL_OPTIONS)

            with yt_dlp.YoutubeDL(options) as ydl:

                print(
                    f"[YTDLP] Searching: {query}"
                )

                data = ydl.extract_info(
                    f"ytsearch1:{query}",
                    download=False
                )

                if not data:
                    return None

                entries = data.get("entries")

                if entries:
                    info = entries[0]

                else:
                    info = data

                if not info:
                    return None

                audio_url = info.get("url")

                if not audio_url:
                    return None

                return {

                    "title":
                        info.get(
                            "title",
                            "Unknown"
                        ),

                    "url":
                        audio_url,

                    "duration":
                        format_duration(
                            info.get("duration")
                        ),

                    "webpage_url":
                        info.get(
                            "webpage_url"
                        )
                }

        except Exception as e:

            print(
                f"[YTDLP ERROR] {repr(e)}"
            )

            return None

    return await loop.run_in_executor(
        None,
        extract
    )


# =========================================================
# DISCORD INTENTS
# =========================================================

intents = discord.Intents.default()

intents.message_content = True
intents.members = True
intents.voice_states = True


# =========================================================
# BOT
# =========================================================

class LomiraBot(discord.Client):

    def __init__(self):

        super().__init__(
            intents=intents
        )

        self.tree = app_commands.CommandTree(
            self
        )

        self.self_role_views = []


    async def setup_hook(self):

        print(
            "[LOMIRA] Syncing commands..."
        )

        try:

            synced = await self.tree.sync()

            print(
                f"[LOMIRA] Synced "
                f"{len(synced)} commands!"
            )

        except Exception as e:

            print(
                f"[SYNC ERROR] {repr(e)}"
            )


client = LomiraBot()


# =========================================================
# READY
# =========================================================

@client.event
async def on_ready():

    print("=" * 50)

    print(
        f"[LOMIRA] Online as {client.user}"
    )

    print(
        f"[LOMIRA] ID: {client.user.id}"
    )

    print(
        f"[LOMIRA] Servers: "
        f"{len(client.guilds)}"
    )

    print("=" * 50)


# =========================================================
# PLAY NEXT
# =========================================================

async def play_next(guild_id):

    guild = client.get_guild(
        guild_id
    )

    if not guild:
        return

    voice_client = guild.voice_client

    if not voice_client:
        return

    if not voice_client.is_connected():
        return

    queue = get_queue(
        guild_id
    )

    if not queue:

        current_songs.pop(
            guild_id,
            None
        )

        return

    song = queue.pop(0)

    current_songs[
        guild_id
    ] = song

    audio_url = song.get("url")

    if not audio_url:

        print(
            "[PLAYER] Missing audio URL"
        )

        await play_next(
            guild_id
        )

        return

    try:

        if not ffmpeg_path:

            print(
                "[PLAYER] FFmpeg not found"
            )

            return

        source = discord.FFmpegPCMAudio(
            audio_url,
            executable=ffmpeg_path,
            **FFMPEG_OPTIONS
        )


        def after_playing(error):

            if error:

                print(
                    f"[PLAYER ERROR] "
                    f"{repr(error)}"
                )

            asyncio.run_coroutine_threadsafe(
                song_finished(guild_id),
                client.loop
            )


        voice_client.play(
            source,
            after=after_playing
        )

        print(
            f"[PLAYER] Playing: "
            f"{song['title']}"
        )

        channel_id = song.get(
            "channel_id"
        )

        if channel_id:

            channel = client.get_channel(
                channel_id
            )

            if channel:

                await channel.send(
                    f"🎵 กำลังเล่น: "
                    f"**{song['title']}**\n"
                    f"⏱️ {song['duration']}"
                )

    except Exception as e:

        print(
            f"[PLAYER ERROR] {repr(e)}"
        )

        await play_next(
            guild_id
        )


# =========================================================
# SONG FINISHED
# =========================================================

async def song_finished(guild_id):

    await asyncio.sleep(1)

    current_songs.pop(
        guild_id,
        None
    )

    guild = client.get_guild(
        guild_id
    )

    if not guild:
        return

    voice_client = guild.voice_client

    if not voice_client:
        return

    if not voice_client.is_connected():
        return

    await play_next(
        guild_id
    )


# =========================================================
# PING
# =========================================================

@client.tree.command(
    name="ping",
    description="เช็กความเร็วการตอบสนองของบอท"
)
async def ping(
    interaction: discord.Interaction
):

    latency = round(
        client.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong! `{latency}ms`"
    )


# =========================================================
# PLAY
# =========================================================

@client.tree.command(
    name="play",
    description="เล่นเพลงหรือเพิ่มเพลงเข้าคิว"
)
@app_commands.describe(
    query="ชื่อเพลง หรือ YouTube/Spotify URL"
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

    if not interaction.user.voice:

        await interaction.followup.send(
            "❌ คุณต้องเข้าห้องเสียงก่อนครับ"
        )

        return

    voice_channel = (
        interaction.user.voice.channel
    )

    voice_client = (
        interaction.guild.voice_client
    )


    # -----------------------------------------------------
    # CONNECT VOICE
    # -----------------------------------------------------

    try:

        if voice_client is None:

            voice_client = await voice_channel.connect(
                self_deaf=True
            )

        elif voice_client.channel != voice_channel:

            await voice_client.move_to(
                voice_channel
            )

    except Exception as e:

        print(
            f"[VOICE ERROR] {repr(e)}"
        )

        await interaction.followup.send(
            "❌ ไม่สามารถเข้าห้องเสียงได้"
        )

        return


    # -----------------------------------------------------
    # SPOTIFY
    # -----------------------------------------------------

    search_term = await asyncio.to_thread(
        get_query_from_input,
        query
    )

    print(
        f"[SEARCH] {search_term}"
    )


    # -----------------------------------------------------
    # YOUTUBE
    # -----------------------------------------------------

    song = await search_song(
        search_term
    )

    if not song:

        await interaction.followup.send(
            "❌ ไม่พบเพลงหรือไม่สามารถดึงเสียงเพลงได้\n"
            "ลองใช้ชื่อเพลง เช่น `Shape of You Ed Sheeran`"
        )

        return


    song["channel_id"] = (
        interaction.channel.id
    )


    guild_id = interaction.guild.id

    queue = get_queue(
        guild_id
    )


    # -----------------------------------------------------
    # PLAY IMMEDIATELY
    # -----------------------------------------------------

    if (
        not voice_client.is_playing()
        and not voice_client.is_paused()
    ):

        queue.append(
            song
        )

        await play_next(
            guild_id
        )

        await interaction.followup.send(
            f"▶️ กำลังเล่น **{song['title']}**\n"
            f"⏱️ {song['duration']}"
        )

        return


    # -----------------------------------------------------
    # ADD QUEUE
    # -----------------------------------------------------

    queue.append(
        song
    )

    position = len(queue)

    await interaction.followup.send(
        f"🎶 เพิ่มลงคิวแล้ว\n"
        f"🎵 **{song['title']}**\n"
        f"⏱️ {song['duration']}\n"
        f"📋 คิวที่ `{position}`"
    )


# =========================================================
# SKIP
# =========================================================

@client.tree.command(
    name="skip",
    description="ข้ามเพลงปัจจุบัน"
)
async def skip(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งในเซิร์ฟเวอร์เท่านั้น"
        )

        return

    voice_client = (
        interaction.guild.voice_client
    )

    if not voice_client:

        await interaction.followup.send(
            "❌ บอทยังไม่ได้อยู่ในห้องเสียง"
        )

        return

    if not voice_client.is_playing():

        await interaction.followup.send(
            "❌ ไม่มีเพลงกำลังเล่น"
        )

        return

    voice_client.stop()

    await interaction.followup.send(
        "⏭️ ข้ามเพลงเรียบร้อย!"
    )


# =========================================================
# QUEUE
# =========================================================

@client.tree.command(
    name="queue",
    description="ดูคิวเพลง"
)
async def queue_command(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งในเซิร์ฟเวอร์เท่านั้น"
        )

        return

    guild_id = interaction.guild.id

    queue = get_queue(
        guild_id
    )

    current = current_songs.get(
        guild_id
    )

    lines = [
        "📋 **คิวเพลง**",
        ""
    ]

    if current:

        lines.append(
            f"▶️ กำลังเล่น: "
            f"**{current['title']}**"
        )

        lines.append("")


    if not queue:

        lines.append(
            "📭 ไม่มีเพลงถัดไปในคิว"
        )

    else:

        for index, song in enumerate(
            queue,
            start=1
        ):

            lines.append(
                f"`{index}.` "
                f"{song['title']} "
                f"({song['duration']})"
            )


    message = "\n".join(
        lines
    )

    if len(message) > 1900:

        message = (
            message[:1900]
            + "\n..."
        )

    await interaction.followup.send(
        message
    )


# =========================================================
# STOP
# =========================================================

@client.tree.command(
    name="stop",
    description="หยุดเพลง ล้างคิว และออกจากห้องเสียง"
)
async def stop(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งในเซิร์ฟเวอร์เท่านั้น"
        )

        return

    guild_id = interaction.guild.id

    get_queue(
        guild_id
    ).clear()

    current_songs.pop(
        guild_id,
        None
    )

    voice_client = (
        interaction.guild.voice_client
    )

    if voice_client:

        try:

            if voice_client.is_playing():

                voice_client.stop()

            if voice_client.is_connected():

                await voice_client.disconnect()

        except Exception as e:

            print(
                f"[STOP ERROR] {repr(e)}"
            )

    await interaction.followup.send(
        "⏹️ หยุดเพลง ล้างคิว และออกจากห้องเสียงแล้ว"
    )


# =========================================================
# LEAVE
# =========================================================

@client.tree.command(
    name="leave",
    description="ให้บอทออกจากห้องเสียง"
)
async def leave(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งในเซิร์ฟเวอร์เท่านั้น"
        )

        return

    voice_client = (
        interaction.guild.voice_client
    )

    if not voice_client:

        await interaction.followup.send(
            "❌ บอทไม่ได้อยู่ในห้องเสียง"
        )

        return

    await voice_client.disconnect()

    await interaction.followup.send(
        "👋 ออกจากห้องเสียงเรียบร้อย"
    )


# =========================================================
# ANNOUNCE
# =========================================================

@client.tree.command(
    name="announce",
    description="ส่งประกาศ"
)
@app_commands.describe(
    message="ข้อความประกาศ",
    channel="ช่องที่จะส่ง"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def announce(
    interaction: discord.Interaction,
    message: str,
    channel: discord.TextChannel = None
):

    await interaction.response.defer(
        ephemeral=True
    )

    target = (
        channel
        or interaction.channel
    )

    embed = discord.Embed(
        title="📢 ประกาศสำคัญ",
        description=message,
        color=discord.Color.blue()
    )

    embed.set_footer(
        text=f"ประกาศโดย {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url
    )

    try:

        await target.send(
            embed=embed
        )

        await interaction.followup.send(
            f"✅ ส่งประกาศไปที่ "
            f"{target.mention} แล้ว",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ส่งข้อความในช่องนี้",
            ephemeral=True
        )


# =========================================================
# CREATE ROLE
# =========================================================

@client.tree.command(
    name="createrole",
    description="สร้างยศใหม่"
)
@app_commands.describe(
    name="ชื่อยศ",
    color="Hex เช่น #FF0000 หรือ red",
    hoist="แสดงยศแยกในสมาชิกหรือไม่"
)
@app_commands.checks.has_permissions(
    manage_roles=True
)
async def createrole(
    interaction: discord.Interaction,
    name: str,
    color: str = None,
    hoist: bool = False
):

    await interaction.response.defer()

    role_color = discord.Color.default()

    if color:

        try:

            color = color.strip()

            colors = {

                "red":
                    discord.Color.red(),

                "blue":
                    discord.Color.blue(),

                "green":
                    discord.Color.green(),

                "yellow":
                    discord.Color.gold(),

                "purple":
                    discord.Color.purple(),

                "orange":
                    discord.Color.orange(),

                "pink":
                    discord.Color.from_rgb(
                        255,
                        105,
                        180
                    )
            }

            if color.lower() in colors:

                role_color = colors[
                    color.lower()
                ]

            elif color.startswith("#"):

                role_color = discord.Color(
                    int(
                        color[1:],
                        16
                    )
                )

            else:

                role_color = discord.Color(
                    int(
                        color,
                        16
                    )
                )

        except Exception:

            await interaction.followup.send(
                "⚠️ สีไม่ถูกต้อง ใช้สีเริ่มต้นแทนครับ"
            )

    try:

        role = await interaction.guild.create_role(
            name=name,
            color=role_color,
            hoist=hoist,
            reason=f"สร้างโดย {interaction.user}"
        )

        await interaction.followup.send(
            f"✅ สร้างยศ "
            f"{role.mention} เรียบร้อย"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ Manage Roles"
        )


# =========================================================
# ADD ROLE
# =========================================================

@client.tree.command(
    name="addrole",
    description="มอบยศให้สมาชิก"
)
@app_commands.describe(
    member="สมาชิก",
    role="ยศ"
)
@app_commands.checks.has_permissions(
    manage_roles=True
)
async def addrole(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role
):

    await interaction.response.defer()

    me = interaction.guild.me

    if me and role >= me.top_role:

        await interaction.followup.send(
            "❌ ยศนี้สูงกว่าหรือเท่ากับยศบอท"
        )

        return

    try:

        await member.add_roles(
            role,
            reason=f"มอบโดย {interaction.user}"
        )

        await interaction.followup.send(
            f"✅ มอบยศ "
            f"{role.mention} ให้ "
            f"{member.mention} แล้ว"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์มอบยศนี้"
        )


# =========================================================
# REMOVE ROLE
# =========================================================

@client.tree.command(
    name="removerole",
    description="ถอดยศจากสมาชิก"
)
@app_commands.describe(
    member="สมาชิก",
    role="ยศ"
)
@app_commands.checks.has_permissions(
    manage_roles=True
)
async def removerole(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role
):

    await interaction.response.defer()

    me = interaction.guild.me

    if me and role >= me.top_role:

        await interaction.followup.send(
            "❌ ยศนี้สูงกว่าหรือเท่ากับยศบอท"
        )

        return

    try:

        await member.remove_roles(
            role,
            reason=f"ถอดโดย {interaction.user}"
        )

        await interaction.followup.send(
            f"✅ ถอดยศ "
            f"{role.mention} จาก "
            f"{member.mention} แล้ว"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ถอดยศนี้"
        )


# =========================================================
# KICK
# =========================================================

@client.tree.command(
    name="kick",
    description="เตะสมาชิก"
)
@app_commands.describe(
    member="สมาชิก",
    reason="เหตุผล"
)
@app_commands.checks.has_permissions(
    kick_members=True
)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    await interaction.response.defer()

    try:

        await member.kick(
            reason=reason
        )

        await interaction.followup.send(
            f"👢 เตะ {member.mention} แล้ว\n"
            f"เหตุผล: {reason}"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่สามารถเตะสมาชิกคนนี้ได้"
        )


# =========================================================
# BAN
# =========================================================

@client.tree.command(
    name="ban",
    description="แบนสมาชิก"
)
@app_commands.describe(
    member="สมาชิก",
    reason="เหตุผล"
)
@app_commands.checks.has_permissions(
    ban_members=True
)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    await interaction.response.defer()

    try:

        await member.ban(
            reason=reason
        )

        await interaction.followup.send(
            f"🔨 แบน {member.mention} แล้ว\n"
            f"เหตุผล: {reason}"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่สามารถแบนสมาชิกคนนี้ได้"
        )


# =========================================================
# UNBAN
# =========================================================

@client.tree.command(
    name="unban",
    description="ปลดแบนด้วย Discord ID"
)
@app_commands.describe(
    user_id="Discord User ID"
)
@app_commands.checks.has_permissions(
    ban_members=True
)
async def unban(
    interaction: discord.Interaction,
    user_id: str
):

    await interaction.response.defer()

    try:

        user = await client.fetch_user(
            int(user_id)
        )

        await interaction.guild.unban(
            user
        )

        await interaction.followup.send(
            f"🔓 ปลดแบน "
            f"{user} เรียบร้อย"
        )

    except ValueError:

        await interaction.followup.send(
            "❌ Discord ID ไม่ถูกต้อง"
        )

    except discord.NotFound:

        await interaction.followup.send(
            "❌ ไม่พบผู้ใช้นี้ในรายการแบน"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ปลดแบน"
        )


# =========================================================
# PURGE
# =========================================================

@client.tree.command(
    name="purge",
    description="ลบข้อความ"
)
@app_commands.describe(
    amount="จำนวน 1-100"
)
@app_commands.checks.has_permissions(
    manage_messages=True
)
async def purge(
    interaction: discord.Interaction,
    amount: int
):

    await interaction.response.defer(
        ephemeral=True
    )

    if amount < 1 or amount > 100:

        await interaction.followup.send(
            "❌ จำนวนต้องอยู่ระหว่าง 1-100",
            ephemeral=True
        )

        return

    try:

        deleted = await interaction.channel.purge(
            limit=amount
        )

        await interaction.followup.send(
            f"🧹 ลบแล้ว "
            f"`{len(deleted)}` ข้อความ",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ลบข้อความ",
            ephemeral=True
        )


# =========================================================
# SELF ROLE BUTTON
# =========================================================

class SelfRoleButton(discord.ui.Button):

    def __init__(self, role: discord.Role):

        super().__init__(
            label=role.name,
            style=discord.ButtonStyle.secondary,
            custom_id=f"selfrole_{role.id}"
        )

        self.role_id = role.id


    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if not interaction.guild:

            await interaction.response.send_message(
                "❌ ใช้ปุ่มนี้ในเซิร์ฟเวอร์เท่านั้น",
                ephemeral=True
            )

            return

        role = interaction.guild.get_role(
            self.role_id
        )

        if not role:

            await interaction.response.send_message(
                "❌ ไม่พบยศนี้แล้ว",
                ephemeral=True
            )

            return

        member = interaction.user

        me = interaction.guild.me

        if not me:

            await interaction.response.send_message(
                "❌ ไม่สามารถตรวจสอบยศบอทได้",
                ephemeral=True
            )

            return

        if role.is_default():

            await interaction.response.send_message(
                "❌ ไม่สามารถรับยศ Everyone ได้",
                ephemeral=True
            )

            return

        if role >= me.top_role:

            await interaction.response.send_message(
                "❌ บอทไม่สามารถมอบยศนี้ได้\n"
                "กรุณาเลื่อนยศบอทให้อยู่สูงกว่ายศนี้",
                ephemeral=True
            )

            return

        try:

            if role in member.roles:

                await member.remove_roles(
                    role,
                    reason="Self Role"
                )

                await interaction.response.send_message(
                    f"➖ ถอดยศ **{role.name}** แล้ว",
                    ephemeral=True
                )

            else:

                await member.add_roles(
                    role,
                    reason="Self Role"
                )

                await interaction.response.send_message(
                    f"✅ รับยศ **{role.name}** เรียบร้อยแล้ว",
                    ephemeral=True
                )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ บอทไม่มีสิทธิ์จัดการยศนี้",
                ephemeral=True
            )

        except Exception as e:

            print(
                f"[SELF ROLE ERROR] {repr(e)}"
            )

            await interaction.response.send_message(
                "❌ เกิดข้อผิดพลาด กรุณาลองใหม่",
                ephemeral=True
            )


# =========================================================
# SELF ROLE VIEW
# =========================================================

class SelfRoleView(discord.ui.View):

    def __init__(self, roles):

        super().__init__(
            timeout=None
        )

        for role in roles:

            if role:

                self.add_item(
                    SelfRoleButton(role)
                )


# =========================================================
# SETUP ROLES
# =========================================================

@client.tree.command(
    name="setup_roles",
    description="สร้างแผงให้สมาชิกกดรับยศ"
)
@app_commands.describe(
    role1="ยศที่ 1",
    role2="ยศที่ 2",
    role3="ยศที่ 3",
    role4="ยศที่ 4",
    role5="ยศที่ 5"
)
@app_commands.checks.has_permissions(
    manage_roles=True
)
async def setup_roles(
    interaction: discord.Interaction,
    role1: discord.Role,
    role2: discord.Role = None,
    role3: discord.Role = None,
    role4: discord.Role = None,
    role5: discord.Role = None
):

    await interaction.response.defer(
        ephemeral=True
    )

    roles = [
        role1,
        role2,
        role3,
        role4,
        role5
    ]

    roles = [
        role
        for role in roles
        if role is not None
    ]


    # ลบยศซ้ำ

    unique_roles = []

    seen = set()

    for role in roles:

        if role.id not in seen:

            unique_roles.append(
                role
            )

            seen.add(
                role.id
            )

    roles = unique_roles


    me = interaction.guild.me

    if not me:

        await interaction.followup.send(
            "❌ ไม่สามารถตรวจสอบยศบอทได้",
            ephemeral=True
        )

        return


    invalid_roles = []

    for role in roles:

        if role.is_default():

            invalid_roles.append(
                f"{role.name} (Everyone)"
            )

        elif role >= me.top_role:

            invalid_roles.append(
                role.name
            )


    if invalid_roles:

        await interaction.followup.send(
            "❌ บอทไม่สามารถมอบยศเหล่านี้ได้:\n"
            + "\n".join(
                f"• `{name}`"
                for name in invalid_roles
            )
            + "\n\n"
            "กรุณาเลื่อนยศบอทให้อยู่สูงกว่ายศเหล่านี้",
            ephemeral=True
        )

        return


    embed = discord.Embed(
        title="🎭 รับยศ",
        description=(
            "กดปุ่มด้านล่างเพื่อรับยศ\n\n"
            "กดปุ่มยศเดิมอีกครั้งเพื่อถอดยศ"
        ),
        color=discord.Color.blurple()
    )


    role_text = []

    for role in roles:

        role_text.append(
            f"• {role.mention}"
        )


    embed.add_field(
        name="ยศที่สามารถรับได้",
        value="\n".join(role_text),
        inline=False
    )

    embed.set_footer(
        text="กดปุ่มเพื่อรับ/ถอดยศ"
    )


    view = SelfRoleView(
        roles
    )


    # เก็บ View ไว้ไม่ให้ถูก garbage collection

    client.self_role_views.append(
        view
    )


    try:

        await interaction.channel.send(
            embed=embed,
            view=view
        )

        await interaction.followup.send(
            "✅ สร้างแผงรับยศเรียบร้อยแล้ว",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ส่งข้อความในห้องนี้",
            ephemeral=True
        )

    except Exception as e:

        print(
            f"[SETUP ROLE ERROR] {repr(e)}"
        )

        await interaction.followup.send(
            "❌ เกิดข้อผิดพลาด กรุณาลองใหม่",
            ephemeral=True
        )


# =========================================================
# COMMAND ERROR
# =========================================================

@client.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    print(
        f"[COMMAND ERROR] {repr(error)}"
    )


    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้"
        )

    elif isinstance(
        error,
        app_commands.BotMissingPermissions
    ):

        message = (
            "❌ บอทไม่มีสิทธิ์ที่จำเป็น"
        )

    else:

        message = (
            "❌ เกิดข้อผิดพลาด "
            "กรุณาลองใหม่"
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

        print(
            f"[ERROR HANDLER] {repr(e)}"
        )


# =========================================================
# GLOBAL ERROR
# =========================================================

@client.event
async def on_error(
    event,
    *args,
    **kwargs
):

    print(
        f"[GLOBAL ERROR] {event}"
    )


# =========================================================
# START BOT
# =========================================================

if __name__ == "__main__":

    keep_alive()

    if not TOKEN:

        print(
            "[ERROR] DISCORD_TOKEN "
            "ไม่พบใน Environment Variables!"
        )

    else:

        print(
            "[LOMIRA] Starting bot..."
        )

        try:

            client.run(
                TOKEN
            )

        except Exception as e:

            print(
                f"[BOT ERROR] {repr(e)}"
            )
