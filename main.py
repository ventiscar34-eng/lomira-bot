import os
import asyncio
import re
from threading import Thread

from flask import Flask

import discord
from discord import app_commands
from discord.ext import commands

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# =========================================================
# 1. Flask Web Server สำหรับ Render
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "LOMIRA Bot is Alive!"


def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    thread = Thread(target=run_web, daemon=True)
    thread.start()


# =========================================================
# 2. Spotify
# =========================================================

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

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
        print(f"[SPOTIFY] Connection failed: {e}")
else:
    print("[SPOTIFY] API keys not found. Spotify links will not be converted.")


def get_query_from_input(query: str) -> str:
    """
    ถ้าเป็น Spotify track URL
    จะดึงชื่อเพลง + ศิลปินออกมา
    """

    spotify_pattern = r"(?:https?://)?open\.spotify\.com/track/([a-zA-Z0-9]+)"
    match = re.search(spotify_pattern, query)

    if not match or not sp:
        return query

    try:
        track_id = match.group(1)

        track = sp.track(track_id)

        name = track["name"]
        artists = ", ".join(
            artist["name"] for artist in track["artists"]
        )

        return f"{name} {artists}"

    except Exception as e:
        print(f"[SPOTIFY] Failed to get track: {e}")
        return query


# =========================================================
# 3. Discord Bot
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class LomiraBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):

        print("[LOMIRA] Syncing slash commands...")

        try:
            synced = await self.tree.sync()
            print(f"[LOMIRA] Synced {len(synced)} commands!")
        except Exception as e:
            print(f"[LOMIRA] Sync error: {e}")


bot = LomiraBot()


# =========================================================
# 4. Queue
# =========================================================

song_queues = {}

# ตัวอย่าง:
# song_queues[guild_id] = [
#     {
#         "title": "เพลง",
#         "query": "เพลง ศิลปิน",
#         "requester": user
#     }
# ]


def get_queue(guild_id):

    if guild_id not in song_queues:
        song_queues[guild_id] = []

    return song_queues[guild_id]


# =========================================================
# 5. YouTube / yt-dlp
# =========================================================

import yt_dlp


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
}


FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


async def search_youtube(query):

    loop = asyncio.get_running_loop()

    def search():

        try:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:

                result = ydl.extract_info(
                    f"ytsearch1:{query}",
                    download=False
                )

                if not result:
                    return None

                entries = result.get("entries")

                if not entries:
                    return None

                video = entries[0]

                return {
                    "title": video.get("title", query),
                    "url": video.get("url"),
                    "webpage_url": video.get("webpage_url"),
                }

        except Exception as e:
            print(f"[YTDLP] Search error: {e}")
            return None

    return await loop.run_in_executor(None, search)


# =========================================================
# 6. เล่นเพลง
# =========================================================

async def play_next(guild_id):

    guild = bot.get_guild(guild_id)

    if not guild:
        return

    queue = get_queue(guild_id)

    voice_client = guild.voice_client

    if not voice_client:
        return

    if voice_client.is_playing() or voice_client.is_paused():
        return

    if not queue:
        try:
            await voice_client.disconnect()
        except Exception:
            pass

        return

    song = queue[0]

    audio_url = song.get("url")

    if not audio_url:
        queue.pop(0)
        await play_next(guild_id)
        return

    source = discord.FFmpegPCMAudio(
        audio_url,
        **FFMPEG_OPTIONS
    )

    def after_playing(error):

        if error:
            print(f"[PLAYER] Error: {error}")

        asyncio.run_coroutine_threadsafe(
            finish_song(guild_id),
            bot.loop
        )

    voice_client.play(
        source,
        after=after_playing
    )

    print(f"[PLAYER] Playing: {song['title']}")


async def finish_song(guild_id):

    queue = get_queue(guild_id)

    if queue:
        queue.pop(0)

    await asyncio.sleep(1)

    await play_next(guild_id)


# =========================================================
# 7. Ready
# =========================================================

@bot.event
async def on_ready():

    print("=" * 40)
    print(f"[LOMIRA] Online as {bot.user}")
    print(f"[LOMIRA] User ID: {bot.user.id}")
    print("=" * 40)


# =========================================================
# 8. /ping
# =========================================================

@bot.tree.command(
    name="ping",
    description="เช็กความเร็วตอบสนองของบอท"
)
async def ping(interaction: discord.Interaction):

    latency = round(bot.latency * 1000)

    await interaction.response.send_message(
        f"🏓 Pong! `{latency}ms`"
    )


# =========================================================
# 9. /play
# =========================================================

@bot.tree.command(
    name="play",
    description="เล่นเพลงหรือเพิ่มเพลงเข้าคิว"
)
@app_commands.describe(
    query="ชื่อเพลง หรือ URL จาก YouTube / Spotify"
)
async def play(
    interaction: discord.Interaction,
    query: str
):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์"
        )
        return

    member = interaction.user

    if not isinstance(member, discord.Member):

        await interaction.followup.send(
            "❌ ไม่สามารถตรวจสอบสมาชิกได้"
        )
        return

    if not member.voice or not member.voice.channel:

        await interaction.followup.send(
            "❌ คุณต้องเข้าห้องเสียงก่อนครับ"
        )
        return

    voice_channel = member.voice.channel

    guild_id = interaction.guild.id

    # Spotify → ชื่อเพลง
    search_query = await asyncio.to_thread(
        get_query_from_input,
        query
    )

    # ค้นหา YouTube
    song = await search_youtube(search_query)

    if not song:

        await interaction.followup.send(
            "❌ ไม่พบเพลงที่ต้องการเล่น"
        )
        return

    queue = get_queue(guild_id)

    song["query"] = search_query
    song["requester"] = interaction.user

    queue.append(song)

    # เชื่อมต่อห้องเสียง
    voice_client = interaction.guild.voice_client

    try:

        if voice_client is None:

            voice_client = await voice_channel.connect()

        elif voice_client.channel != voice_channel:

            await voice_client.move_to(voice_channel)

    except Exception as e:

        print(f"[VOICE] Connection error: {e}")

        if queue and queue[-1] == song:
            queue.pop()

        await interaction.followup.send(
            "❌ ไม่สามารถเข้าห้องเสียงได้\n"
            "ตรวจสอบสิทธิ์ Connect และ Speak ของบอทด้วยครับ"
        )
        return

    # ถ้ายังไม่มีเพลงเล่น
    if not voice_client.is_playing() and not voice_client.is_paused():

        await play_next(guild_id)

        await interaction.followup.send(
            f"🎵 กำลังเล่น **{song['title']}**"
        )

    else:

        position = len(queue)

        await interaction.followup.send(
            f"🎶 เพิ่ม **{song['title']}** ลงคิวแล้ว\n"
            f"📋 ลำดับคิว: `{position}`"
        )


# =========================================================
# 10. /skip
# =========================================================

@bot.tree.command(
    name="skip",
    description="ข้ามเพลงปัจจุบัน"
)
async def skip(interaction: discord.Interaction):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งนี้ในเซิร์ฟเวอร์เท่านั้น"
        )
        return

    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():

        await interaction.followup.send(
            "❌ บอทยังไม่ได้อยู่ในห้องเสียง"
        )
        return

    if not voice_client.is_playing():

        await interaction.followup.send(
            "❌ ตอนนี้ไม่มีเพลงกำลังเล่น"
        )
        return

    voice_client.stop()

    await interaction.followup.send(
        "⏭️ ข้ามเพลงเรียบร้อย!"
    )


# =========================================================
# 11. /queue
# =========================================================

@bot.tree.command(
    name="queue",
    description="ดูคิวเพลง"
)
async def queue_command(interaction: discord.Interaction):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งนี้ในเซิร์ฟเวอร์เท่านั้น"
        )
        return

    queue = get_queue(interaction.guild.id)

    if not queue:

        await interaction.followup.send(
            "📭 ตอนนี้ไม่มีเพลงในคิว"
        )
        return

    lines = []

    for i, song in enumerate(queue, start=1):

        if i == 1:
            lines.append(
                f"▶️ **{i}. {song['title']}**"
            )
        else:
            lines.append(
                f"`{i}.` {song['title']}"
            )

    message = (
        "📋 **คิวเพลง**\n\n"
        + "\n".join(lines)
    )

    # Discord จำกัดข้อความประมาณ 2000 ตัวอักษร
    if len(message) > 1900:

        message = message[:1900] + "\n..."

    await interaction.followup.send(message)


# =========================================================
# 12. /stop
# =========================================================

@bot.tree.command(
    name="stop",
    description="หยุดเพลง ล้างคิว และออกจากห้องเสียง"
)
async def stop(interaction: discord.Interaction):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งนี้ในเซิร์ฟเวอร์เท่านั้น"
        )
        return

    guild_id = interaction.guild.id

    queue = get_queue(guild_id)
    queue.clear()

    voice_client = interaction.guild.voice_client

    if voice_client:

        try:

            if voice_client.is_playing():
                voice_client.stop()

            await voice_client.disconnect()

        except Exception as e:

            print(f"[VOICE] Disconnect error: {e}")

    await interaction.followup.send(
        "⏹️ หยุดเพลง ล้างคิว และออกจากห้องเสียงแล้ว"
    )


# =========================================================
# 13. /leave
# =========================================================

@bot.tree.command(
    name="leave",
    description="ให้บอทออกจากห้องเสียง"
)
async def leave(interaction: discord.Interaction):

    await interaction.response.defer()

    if not interaction.guild:

        await interaction.followup.send(
            "❌ ใช้คำสั่งนี้ในเซิร์ฟเวอร์เท่านั้น"
        )
        return

    voice_client = interaction.guild.voice_client

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
# 14. Admin /announce
# =========================================================

@bot.tree.command(
    name="announce",
    description="ส่งประกาศ"
)
@app_commands.describe(
    message="ข้อความประกาศ",
    channel="ช่องที่จะส่งประกาศ"
)
@app_commands.checks.has_permissions(administrator=True)
async def announce(
    interaction: discord.Interaction,
    message: str,
    channel: discord.TextChannel = None
):

    await interaction.response.defer(ephemeral=True)

    target_channel = channel or interaction.channel

    embed = discord.Embed(
        title="📢 ประกาศสำคัญ",
        description=message,
        color=discord.Color.blue()
    )

    embed.set_footer(
        text=f"ประกาศโดย {interaction.user.display_name}"
    )

    try:

        await target_channel.send(embed=embed)

        await interaction.followup.send(
            f"✅ ส่งประกาศไปที่ {target_channel.mention} แล้ว",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ส่งข้อความในช่องนี้",
            ephemeral=True
        )


# =========================================================
# 15. Admin /addrole
# =========================================================

@bot.tree.command(
    name="addrole",
    description="มอบยศให้สมาชิก"
)
@app_commands.describe(
    member="สมาชิก",
    role="ยศ"
)
@app_commands.checks.has_permissions(manage_roles=True)
async def addrole(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role
):

    await interaction.response.defer()

    if role >= interaction.guild.me.top_role:

        await interaction.followup.send(
            "❌ บอทไม่สามารถมอบยศที่สูงกว่าหรือเท่ากับยศของตัวเองได้"
        )
        return

    try:

        await member.add_roles(role)

        await interaction.followup.send(
            f"✅ มอบยศ {role.mention} ให้ {member.mention} แล้ว"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์มอบยศนี้"
        )


# =========================================================
# 16. Admin /removerole
# =========================================================

@bot.tree.command(
    name="removerole",
    description="ถอดยศออกจากสมาชิก"
)
@app_commands.describe(
    member="สมาชิก",
    role="ยศ"
)
@app_commands.checks.has_permissions(manage_roles=True)
async def removerole(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role
):

    await interaction.response.defer()

    if role >= interaction.guild.me.top_role:

        await interaction.followup.send(
            "❌ บอทไม่สามารถถอดยศที่สูงกว่าหรือเท่ากับยศของตัวเองได้"
        )
        return

    try:

        await member.remove_roles(role)

        await interaction.followup.send(
            f"✅ ถอดยศ {role.mention} จาก {member.mention} แล้ว"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ถอดยศนี้"
        )


# =========================================================
# 17. Admin /kick
# =========================================================

@bot.tree.command(
    name="kick",
    description="เตะสมาชิกออกจากเซิร์ฟเวอร์"
)
@app_commands.describe(
    member="สมาชิกที่ต้องการเตะ",
    reason="เหตุผล"
)
@app_commands.checks.has_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    await interaction.response.defer()

    try:

        await member.kick(reason=reason)

        await interaction.followup.send(
            f"👢 เตะ {member.mention} แล้ว\n"
            f"เหตุผล: {reason}"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ ไม่สามารถเตะสมาชิกคนนี้ได้"
        )


# =========================================================
# 18. Admin /ban
# =========================================================

@bot.tree.command(
    name="ban",
    description="แบนสมาชิกออกจากเซิร์ฟเวอร์"
)
@app_commands.describe(
    member="สมาชิกที่ต้องการแบน",
    reason="เหตุผล"
)
@app_commands.checks.has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "ไม่ได้ระบุเหตุผล"
):

    await interaction.response.defer()

    try:

        await member.ban(reason=reason)

        await interaction.followup.send(
            f"🔨 แบน {member.mention} แล้ว\n"
            f"เหตุผล: {reason}"
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ ไม่สามารถแบนสมาชิกคนนี้ได้"
        )


# =========================================================
# 19. Admin /unban
# =========================================================

@bot.tree.command(
    name="unban",
    description="ปลดแบนสมาชิกด้วย Discord ID"
)
@app_commands.describe(
    user_id="Discord User ID"
)
@app_commands.checks.has_permissions(ban_members=True)
async def unban(
    interaction: discord.Interaction,
    user_id: str
):

    await interaction.response.defer()

    try:

        user = await bot.fetch_user(int(user_id))

        await interaction.guild.unban(user)

        await interaction.followup.send(
            f"🔓 ปลดแบน {user} เรียบร้อย"
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

    except Exception as e:

        print(f"[UNBAN] Error: {e}")

        await interaction.followup.send(
            "❌ เกิดข้อผิดพลาดในการปลดแบน"
        )


# =========================================================
# 20. Admin /purge
# =========================================================

@bot.tree.command(
    name="purge",
    description="ลบข้อความ"
)
@app_commands.describe(
    amount="จำนวนข้อความ 1-100"
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
            f"🧹 ลบข้อความแล้ว `{len(deleted)}` ข้อความ",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์ลบข้อความ",
            ephemeral=True
        )


# =========================================================
# 21. Error Handler
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(error, app_commands.MissingPermissions):

        message = "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้ครับ"

    elif isinstance(error, app_commands.BotMissingPermissions):

        message = "❌ บอทไม่มีสิทธิ์ที่จำเป็นสำหรับคำสั่งนี้ครับ"

    elif isinstance(error, app_commands.CommandOnCooldown):

        message = "⏳ กรุณารอสักครู่แล้วลองใหม่"

    else:

        print(f"[COMMAND ERROR] {repr(error)}")

        message = "❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้งครับ"

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

        print(f"[ERROR HANDLER] {e}")


# =========================================================
# 22. Global Error
# =========================================================

@bot.event
async def on_error(event, *args, **kwargs):

    import traceback

    print(f"[GLOBAL ERROR] Event: {event}")

    traceback.print_exc()


# =========================================================
# 23. Start
# =========================================================

keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:

    print("[ERROR] DISCORD_TOKEN ไม่พบใน Environment Variables!")

else:

    print("[LOMIRA] Starting bot...")

    try:
        bot.run(TOKEN)

    except Exception as e:
        print(f"[BOT ERROR] {e}")
