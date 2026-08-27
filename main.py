import os
import asyncio
import re
from flask import Flask
from threading import Thread
import discord
from discord import app_commands
import yt_dlp

# ไลบรารีสำหรับ Spotify
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# ==========================================
# 1. Web Server (Flask) สำหรับ Render
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "LOMIRA Bot is Alive!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# ==========================================
# 2. ตั้งค่า Spotify API & ระบบค้นหาเพลง
# ==========================================
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')

sp = None
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET
    ))

def get_query_from_input(query: str) -> str:
    spotify_pattern = r'open\.spotify\.com/track/([a-zA-Z0-9]+)'
    match = re.search(spotify_pattern, query)
    
    if match and sp:
        track_id = match.group(1)
        try:
            track_info = sp.track(track_id)
            track_name = track_info['name']
            artist_name = track_info['artists'][0]['name']
            return f"{track_name} {artist_name}"
        except Exception:
            pass
    return query

# ตัวเก็บคิวเพลงแต่ละเซิร์ฟเวอร์
song_queues = {}

# ตั้งค่า yt-dlp และ ffmpeg
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

def format_duration(seconds):
    if not seconds:
        return "ไม่ทราบเวลา"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"

# ==========================================
# 3. ตั้งค่า Discord Bot Client
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

class LomiraBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        print("[LOMIRA] Syncing commands...")
        await self.tree.sync()
        print("[LOMIRA] Synced commands successfully!")

client = LomiraBot()

@client.event
async def on_ready():
    print(f"[LOMIRA] Online as {client.user}")

# ฟังก์ชันสำหรับเล่นเพลงในคิวต่อไป
def play_next(guild_id, interaction):
    if guild_id in song_queues and len(song_queues[guild_id]) > 0:
        song_info = song_queues[guild_id].pop(0)
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_connected():
            source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
            voice_client.play(source, after=lambda e: play_next(guild_id, interaction))
            
            # ส่งข้อความบอกเพลงถัดไป
            asyncio.run_coroutine_threadsafe(
                interaction.channel.send(f"🎵 กำลังเล่น: **{song_info['title']}**\nความยาว: {song_info['duration']}"),
                client.loop
            )

# ==========================================
# 4. คำสั่ง Slash Commands
# ==========================================

# --- หมวดทั่วไป ---
@client.tree.command(name="ping", description="เช็กความเร็วการตอบสนองของบอท")
async def ping(interaction: discord.Interaction):
    await interaction.response.defer()
    latency = round(client.latency * 1000)
    await interaction.followup.send(f"🏓 พิง! ความเร็วตอบสนอง: {latency} มิลลิวินาที")

# --- หมวดเพลง ---
@client.tree.command(name="play", description="สั่งเล่นเพลงหรือเพิ่มเข้าคิว")
@app_commands.describe(query="ใส่ชื่อเพลง หรือ ลิงก์ YouTube/Spotify")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ คุณต้องเชื่อมต่อเข้าไปในห้องเสียง (Voice Channel) ก่อนใช้คำสั่งนี้ครับ!")
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    # เชื่อมต่อเข้าห้องเสียงพร้อมเปิดโหมด Deafen (หูฟังสีแดง)
    try:
        if voice_client is None:
            voice_client = await voice_channel.connect(self_deaf=True)
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
            await interaction.guild.change_voice_state(channel=voice_channel, self_deaf=True)
    except Exception as e:
        await interaction.followup.send(f"❌ ไม่สามารถเชื่อมต่อห้องเสียงได้: {e}")
        return

    # ดึงข้อมูลเพลง
    search_term = get_query_from_input(query)
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch:{search_term}", download=False))
        if 'entries' in data and len(data['entries']) > 0:
            info = data['entries'][0]
        else:
            info = data
    except Exception as e:
        await interaction.followup.send("❌ ไม่พบข้อมูลเพลง หรือเกิดข้อผิดพลาดในการดึงเพลง")
        return

    song_info = {
        'title': info.get('title', 'Unknown Title'),
        'url': info.get('url'),
        'duration': format_duration(info.get('duration'))
    }

    guild_id = interaction.guild_id
    if guild_id not in song_queues:
        song_queues[guild_id] = []

    # ถ้าบอทไม่ได้เล่นเพลงอยู่ ให้เล่นทันที
    if not voice_client.is_playing() and not voice_client.is_paused():
        source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
        voice_client.play(source, after=lambda e: play_next(guild_id, interaction))
        await interaction.followup.send(
            f"➕ | **เพิ่มเพลง:** {song_info['title']}\n"
            f"กำลังเล่น **{song_info['title']}**\n"
            f"ความยาว: {song_info['duration']}"
        )
    else:
        song_queues[guild_id].append(song_info)
        await interaction.followup.send(
            f"🎶 เพิ่มลงคิวเรียบร้อย: **{song_info['title']}** (คิวที่ {len(song_queues[guild_id])})\n"
            f"ความยาว: {song_info['duration']}"
        )

@client.tree.command(name="skip", description="ข้ามเพลงปัจจุบันไปเพลงถัดไป")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer()
    voice_client = interaction.guild.voice_client
    
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.followup.send("⏭️ ข้ามเพลงเรียบร้อยแล้ว!")
    else:
        await interaction.followup.send("❌ ไม่มีเพลงที่กำลังเล่นอยู่ครับ")

@client.tree.command(name="queue", description="ดูรายการเพลงทั้งหมดในคิว")
async def queue(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild_id
    
    if guild_id in song_queues and len(song_queues[guild_id]) > 0:
        msg = "**📋 รายการคิวเพลงปัจจุบัน:**\n"
        for i, song in enumerate(song_queues[guild_id], 1):
            msg += f"{i}. {song['title']} ({song['duration']})\n"
        await interaction.followup.send(msg)
    else:
        await interaction.followup.send("📭 ตอนนี้ไม่มีเพลงอยู่ในคิวครับ")

@client.tree.command(name="stop", description="หยุดเล่นเพลง ล้างคิวทั้งหมด และออกจากห้องเสียง")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild_id
    
    if guild_id in song_queues:
        song_queues[guild_id].clear()
        
    voice_client = interaction.guild.voice_client
    if voice_client:
        if voice_client.is_playing():
            voice_client.stop()
        if voice_client.is_connected():
            await voice_client.disconnect()
        await interaction.followup.send("⏹️ หยุดเล่นเพลง ล้างคิวทั้งหมด และออกจากห้องเสียงเรียบร้อยครับ")
    else:
        await interaction.followup.send("⏹️ หยุดเล่นเพลงและล้างคิวเรียบร้อยครับ")

@client.tree.command(name="leave", description="บังคับให้บอทออกจากห้องเสียง")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer()
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await interaction.followup.send("👋 ออกจากห้องเสียงเรียบร้อยแล้วครับ!")
    else:
        await interaction.followup.send("❌ บอทไม่ได้อยู่ในห้องเสียงครับ")

# --- หมวดแอดมิน ---
@client.tree.command(name="announce", description="คำสั่งผู้ดูแลระบบ: ส่งข้อความประกาศ")
@app_commands.describe(message="ข้อความที่ต้องการประกาศ", channel="เลือกช่องที่ต้องการส่งประกาศ")
@app_commands.checks.has_permissions(administrator=True)
async def announce(interaction: discord.Interaction, message: str, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    target_channel = channel or interaction.channel
    embed = discord.Embed(title="📢 ประกาศสำคัญ", description=message, color=discord.Color.blue())
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await target_channel.send(embed=embed)
    await interaction.followup.send(f"✅ ส่งประกาศไปยังช่อง {target_channel.mention} เรียบร้อยแล้วครับ", ephemeral=True)

@client.tree.command(name="createrole", description="คำสั่งผู้ดูแลระบบ: สร้างยศใหม่ในเซิร์ฟเวอร์")
@app_commands.describe(
    name="ชื่อยศที่ต้องการสร้าง",
    color="รหัสสี Hex (เช่น #FF0000 หรือ red, blue, green)",
    hoist="แสดงยศนี้แยกในรายการสมาชิกหรือไม่ (True/False)"
)
@app_commands.checks.has_permissions(manage_roles=True)
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
            if color.startswith("#"):
                color_code = int(color.lstrip("#"), 16)
                role_color = discord.Color(color_code)
            elif hasattr(discord.Color, color.lower()):
                role_color = getattr(discord.Color, color.lower())()
        except Exception:
            await interaction.followup.send("⚠️ รหัสสีไม่ถูกต้อง ระบบจะใช้สีเริ่มต้นให้แทนครับ")

    try:
        new_role = await interaction.guild.create_role(
            name=name, 
            color=role_color, 
            hoist=hoist,
            reason=f"สร้างโดย {interaction.user.name}"
        )
        await interaction.followup.send(f"✅ สร้างยศ {new_role.mention} เรียบร้อยแล้วครับ!")
    except Exception as e:
        await interaction.followup.send(f"❌ ไม่สามารถสร้างยศได้: {e}")

@client.tree.command(name="addrole", description="คำสั่งผู้ดูแลระบบ: มอบยศให้สมาชิก")
@app_commands.describe(member="เลือกสมาชิก", role="เลือกยศที่ต้องการมอบให้")
@app_commands.checks.has_permissions(manage_roles=True)
async def addrole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await interaction.response.defer()
    await member.add_roles(role)
    await interaction.followup.send(f"✅ มอบยศ {role.mention} ให้กับ {member.mention} เรียบร้อยแล้วครับ")

@client.tree.command(name="removerole", description="คำสั่งผู้ดูแลระบบ: ถอดยศออกจากสมาชิก")
@app_commands.describe(member="เลือกสมาชิก", role="เลือกยศที่ต้องการถอดออก")
@app_commands.checks.has_permissions(manage_roles=True)
async def removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await interaction.response.defer()
    await member.remove_roles(role)
    await interaction.followup.send(f"❌ ถอดยศ {role.mention} ออกจาก {member.mention} เรียบร้อยแล้วครับ")

@client.tree.command(name="kick", description="คำสั่งผู้ดูแลระบบ: เตะสมาชิกออกจากเซิร์ฟเวอร์")
@app_commands.describe(member="เลือกสมาชิกที่ต้องการเตะ", reason="ระบุเหตุผล (ถ้ามี)")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "ไม่ได้ระบุเหตุผล"):
    await interaction.response.defer()
    await member.kick(reason=reason)
    await interaction.followup.send(f"👢 เตะสมาชิก {member.mention} ออกจากเซิร์ฟเวอร์เรียบร้อยแล้ว (เหตุผล: {reason})")

@client.tree.command(name="ban", description="คำสั่งผู้ดูแลระบบ: แบนสมาชิกออกจากเซิร์ฟเวอร์")
@app_commands.describe(member="เลือกสมาชิกที่ต้องการแบน", reason="ระบุเหตุผล (ถ้ามี)")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "ไม่ได้ระบุเหตุผล"):
    await interaction.response.defer()
    await member.ban(reason=reason)
    await interaction.followup.send(f"🔨 แบนสมาชิก {member.mention} เรียบร้อยแล้ว (เหตุผล: {reason})")

@client.tree.command(name="unban", description="คำสั่งผู้ดูแลระบบ: ปลดแบนสมาชิก (ใส่ Discord ID)")
@app_commands.describe(user_id="ใส่ Discord User ID ของคนที่ต้องการปลดแบน")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    try:
        user = await client.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.followup.send(f"🔓 ปลดแบนคุณ {user.name} เรียบร้อยแล้วครับ")
    except Exception:
        await interaction.followup.send("❌ ไม่พบผู้ใช้รายนี้ในรายการแบน หรือ ID ไม่ถูกต้อง")

@client.tree.command(name="purge", description="คำสั่งผู้ดูแลระบบ: ลบข้อความในช่องแชทตามจำนวนที่ระบุ")
@app_commands.describe(amount="จำนวนข้อความที่ต้องการลบ (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    if amount < 1 or amount > 100:
        await interaction.followup.send("❌ กรุณาระบุจำนวนข้อความระหว่าง 1 ถึง 100 เท่านั้น", ephemeral=True)
        return
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 ลบข้อความเรียบร้อยแล้วจำนวน {len(deleted)} ข้อความ", ephemeral=True)

@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("❌ คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้!", ephemeral=True)

# ==========================================
# 5. เริ่มการทำงานของ Bot
# ==========================================
keep_alive()

token = os.getenv('DISCORD_TOKEN')
if token:
    client.run(token)
else:
    print("[ERROR] ไม่พบ DISCORD_TOKEN ใน Environment Variables!")
