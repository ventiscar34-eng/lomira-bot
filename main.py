import os
import re
import json
import sqlite3
import asyncio
import datetime
from threading import Thread

from flask import Flask
import discord
from discord import app_commands
import yt_dlp
import imageio_ffmpeg
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# =========================================================
# DATABASE SETUP (SQLite)
# =========================================================

DB_NAME = "bot_config.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Table for Welcome/Goodbye/Boost Configs
    c.execute('''
        CREATE TABLE IF NOT EXISTS server_configs (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel_id INTEGER,
            welcome_msg TEXT,
            welcome_img TEXT,
            welcome_enabled INTEGER DEFAULT 0,
            goodbye_channel_id INTEGER,
            goodbye_msg TEXT,
            goodbye_img TEXT,
            goodbye_enabled INTEGER DEFAULT 0,
            boost_channel_id INTEGER,
            boost_msg TEXT,
            boost_img TEXT,
            boost_enabled INTEGER DEFAULT 0
        )
    ''')
    # Table for Warnings
    c.execute('''
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            moderator_id INTEGER,
            timestamp TEXT
        )
    ''')
    # Table for Persistent Self Role Panels
    c.execute('''
        CREATE TABLE IF NOT EXISTS self_roles (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            channel_id INTEGER,
            title TEXT,
            description TEXT,
            image_url TEXT,
            roles_json TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    conn.close()

def db_fetch_one(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(query, params)
    res = c.fetchone()
    conn.close()
    return res

def db_fetch_all(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(query, params)
    res = c.fetchall()
    conn.close()
    return res

# =========================================================
# CONFIG & ENVIRONMENT VARIABLES
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# =========================================================
# FLASK WEB SERVER (FOR RENDER KEEP ALIVE)
# =========================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "LOMIRA Multi-System Bot is Running Online!"

@app.route("/health")
def health():
    return "OK", 200

def run_web():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# =========================================================
# SPOTIFY & YOUTUBE / FFMPEG SETUP
# =========================================================

sp = None
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        ))
        print("[SPOTIFY] Successfully Connected!")
    except Exception as e:
        print(f"[SPOTIFY ERROR] {e}")

try:
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as e:
    ffmpeg_path = "ffmpeg"

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch1',
    'source_address': '0.0.0.0',
    'nocheckcertificate': True,
    'geo_bypass': True,
    'socket_timeout': 15,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

def get_spotify_track_info(url_or_query: str):
    match = re.search(r"open\.spotify\.com/track/([a-zA-Z0-9]+)", url_or_query)
    if match and sp:
        try:
            track = sp.track(match.group(1))
            name = track.get("name", "")
            artists = ", ".join([a["name"] for a in track.get("artists", [])])
            return f"{name} {artists}"
        except Exception as e:
            print(f"[SPOTIFY FETCH ERROR] {e}")
    return url_or_query

async def search_yt(query: str):
    loop = asyncio.get_running_loop()
    def _extract():
        query_term = get_spotify_track_info(query)
        target = query_term if query_term.startswith("http") else f"ytsearch1:{query_term}"
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            try:
                info = ydl.extract_info(target, download=False)
                if 'entries' in info and len(info['entries']) > 0:
                    info = info['entries'][0]
                return {
                    'title': info.get('title', 'Unknown Title'),
                    'url': info.get('url'),
                    'duration': info.get('duration', 0),
                    'webpage_url': info.get('webpage_url', '')
                }
            except Exception as e:
                print(f"[YT-DLP SEARCH ERROR] {e}")
                return None
    return await loop.run_in_executor(None, _extract)

# =========================================================
# MUSIC SYSTEM DATA STRUCTURES
# =========================================================

music_queues = {}
now_playing = {}
loop_status = {}
volume_status = {}

def get_queue(guild_id: int):
    if guild_id not in music_queues:
        music_queues[guild_id] = []
    return music_queues[guild_id]

def format_sec(sec):
    if not sec: return "Live / ไม่ทราบเวลา"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

# =========================================================
# PERSISTENT SELF ROLE VIEWS & BUTTONS
# =========================================================

class SelfRoleButton(discord.ui.Button):
    def __init__(self, role_id: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"persistent_role_{role_id}"
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
        
        role = guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("❌ ไม่พบยศนี้ในเซิร์ฟเวอร์", ephemeral=True)
        
        bot_member = guild.me
        if bot_member.top_role <= role:
            return await interaction.response.send_message("❌ บอทไม่มีสิทธิ์จัดการยศนี้ (ยศนี้อยู่สูงกว่าหรือเท่ากับยศของบอท)", ephemeral=True)

        user = interaction.user
        try:
            if role in user.roles:
                await user.remove_roles(role)
                await interaction.response.send_message(f"➖ ถอดยศ **{role.name}** เรียบร้อยแล้ว", ephemeral=True)
            else:
                await user.add_roles(role)
                await interaction.response.send_message(f"➕ รับยศ **{role.name}** เรียบร้อยแล้ว", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ บอทไม่มีสิทธิ์จัดการยศ (ขาด Manage Roles)", ephemeral=True)

class DynamicSelfRoleView(discord.ui.View):
    def __init__(self, roles_data):
        super().__init__(timeout=None)
        for role_id, role_name in roles_data:
            self.add_item(SelfRoleButton(int(role_id), role_name))

# =========================================================
# DISCORD CLIENT CLASS
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

class LomiraBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register Persistent Views for Self Roles from Database
        rows = db_fetch_all("SELECT roles_json FROM self_roles")
        for row in rows:
            try:
                roles_data = json.loads(row[0])
                self.add_view(DynamicSelfRoleView(roles_data))
            except Exception as e:
                print(f"[RELOAD VIEW ERROR] {e}")

        # Set Activity
        activity = discord.Activity(type=discord.ActivityType.listening, name="/play")
        await self.change_presence(activity=activity)

        print("[BOT SETUP] Registering & Syncing Slash Commands...")
        synced = await self.tree.sync()
        print(f"[BOT SETUP] Synced {len(synced)} Slash Commands successfully!")

client = LomiraBot()

@client.event
async def on_ready():
    print("=" * 60)
    print(f"🤖 Bot Online as: {client.user} (ID: {client.user.id})")
    print(f"🌐 Connected Servers: {len(client.guilds)}")
    print(f"🎵 Activity set to: Listening to /play")
    print("=" * 60)

# =========================================================
# GLOBAL ERROR HANDLER FOR SLASH COMMANDS
# =========================================================

@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ คุณไม่มีสิทธิ์ (Permission) ในการใช้คำสั่งนี้"
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "❌ บอทขาดสิทธิ์ (Bot Permission) ในการทำงานตามคำสั่งนี้"
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "❌ คำสั่งนี้สามารถใช้งานในเซิร์ฟเวอร์เท่านั้น"
    else:
        msg = f"❌ เกิดข้อผิดพลาด: {str(error)}"
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# =========================================================
# MUSIC PLAYBACK LOGIC
# =========================================================

async def play_next(guild: discord.Guild, text_channel: discord.TextChannel):
    guild_id = guild.id
    vc = guild.voice_client

    if not vc or not vc.is_connected():
        return

    q = get_queue(guild_id)

    # Check loop logic
    if loop_status.get(guild_id, False) and guild_id in now_playing:
        current = now_playing[guild_id]
    else:
        if len(q) == 0:
            now_playing.pop(guild_id, None)
            return
        current = q.pop(0)
        now_playing[guild_id] = current

    try:
        vol = volume_status.get(guild_id, 1.0)
        source = discord.FFmpegPCMAudio(current['url'], executable=ffmpeg_path, **FFMPEG_OPTIONS)
        transformed_source = discord.PCMVolumeTransformer(source, volume=vol)

        def after_finish(err):
            if err:
                print(f"[AUDIO ERROR] {err}")
            fut = play_next(guild, text_channel)
            asyncio.run_coroutine_threadsafe(fut, client.loop)

        vc.play(transformed_source, after=after_finish)
        
        embed = discord.Embed(
            title="🎵 กำลังเล่นเพลง",
            description=f"[{current['title']}]({current['webpage_url']})",
            color=discord.Color.blue()
        )
        embed.add_field(name="⏱️ ความยาว", value=format_sec(current['duration']), inline=True)
        embed.add_field(name="🔁 วนซ้ำ", value="เปิดอยู่" if loop_status.get(guild_id) else "ปิดอยู่", inline=True)
        await text_channel.send(embed=embed)

    except Exception as e:
        print(f"[PLAYBACK ERROR] {e}")
        await text_channel.send(f"❌ เกิดข้อผิดพลาดขณะเล่นเพลง: {e}")
        await play_next(guild, text_channel)

# =========================================================
# MUSIC SLASH COMMANDS
# =========================================================

@client.tree.command(name="play", description="เล่นเพลงจาก YouTube, Spotify หรือค้นหาชื่อเพลง")
@app_commands.describe(query="ลิงก์ YouTube, ลิงก์ Spotify หรือชื่อเพลงที่ต้องการค้นหา")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์")

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ คุณต้องเชื่อมต่อในห้องเสียง (Voice Channel) ก่อน")

    target_channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    # Connect or Move Voice Channel
    try:
        if not vc:
            vc = await target_channel.connect(self_deaf=True)
        elif vc.channel != target_channel:
            await vc.move_to(target_channel)
    except Exception as e:
        return await interaction.followup.send(f"❌ ไม่สามารถเข้าห้องเสียงได้: {e}")

    song_info = await search_yt(query)
    if not song_info or not song_info.get("url"):
        return await interaction.followup.send("❌ ไม่พบเพลงหรือไม่สามารถดึงข้อมูลเพลงได้ ลองตรวจสอบลิงก์หรือคำค้นหา")

    q = get_queue(interaction.guild.id)
    
    if vc.is_playing() or vc.is_paused():
        q.append(song_info)
        embed = discord.Embed(
            title="🎶 เพิ่มเพลงลงคิวแล้ว",
            description=f"[{song_info['title']}]({song_info['webpage_url']})",
            color=discord.Color.green()
        )
        embed.add_field(name="⏱️ ความยาว", value=format_sec(song_info['duration']))
        embed.add_field(name="📋 คิวที่", value=str(len(q)))
        await interaction.followup.send(embed=embed)
    else:
        q.append(song_info)
        await interaction.followup.send("🔎 กำลังเตรียมเล่นเพลง...")
        await play_next(interaction.guild, interaction.channel)

@client.tree.command(name="queue", description="ดูรายการคิวเพลงที่รอเล่น")
async def queue_cmd(interaction: discord.Interaction):
    q = get_queue(interaction.guild.id)
    curr = now_playing.get(interaction.guild.id)

    if not curr and len(q) == 0:
        return await interaction.response.send_message("📭 ไม่มีเพลงอยู่ในคิวขณะนี้", ephemeral=True)

    desc = ""
    if curr:
        desc += f"**▶️ กำลังเล่นปัจจุบัน:**\n[{curr['title']}]({curr['webpage_url']}) | `{format_sec(curr['duration'])}`\n\n**📋 รายการคิวถัดไป:**\n"
    
    if len(q) == 0:
        desc += "ไม่มีเพลงถัดไปในคิว"
    else:
        for idx, s in enumerate(q[:10], start=1):
            desc += f"`{idx}.` [{s['title']}]({s['webpage_url']}) | `{format_sec(s['duration'])}`\n"
        if len(q) > 10:
            desc += f"\n...และอีก {len(q) - 10} เพลง"

    embed = discord.Embed(title=f"🎶 คิวเพลงใน {interaction.guild.name}", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="skip", description="ข้ามเพลงที่กำลังเล่นอยู่")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ ไม่มีเพลงที่กำลังเล่นอยู่", ephemeral=True)

    loop_status[interaction.guild.id] = False
    vc.stop()
    await interaction.response.send_message("⏭️ ข้ามเพลงเรียบร้อยแล้ว")

@client.tree.command(name="pause", description="หยุดเล่นเพลงชั่วคราว")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ ไม่มีเพลงที่กำลังเล่นอยู่", ephemeral=True)
    vc.pause()
    await interaction.response.send_message("⏸️ หยุดเพลงชั่วคราวแล้ว")

@client.tree.command(name="resume", description="เล่นเพลงต่อจากที่หยุดชั่วคราว")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("❌ เพลงไม่ได้ถูกหยุดชั่วคราว", ephemeral=True)
    vc.resume()
    await interaction.response.send_message("▶️ เล่นเพลงต่อแล้ว")

@client.tree.command(name="stop", description="หยุดเล่นเพลงและล้างคิวทั้งหมด")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    get_queue(guild_id).clear()
    now_playing.pop(guild_id, None)
    loop_status[guild_id] = False

    vc = interaction.guild.voice_client
    if vc:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
    await interaction.response.send_message("⏹️ หยุดเพลงและล้างคิวทั้งหมดเรียบร้อยแล้ว")

@client.tree.command(name="leave", description="ตัดการเชื่อมต่อและออกจากห้องเสียง")
async def leave(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    get_queue(guild_id).clear()
    now_playing.pop(guild_id, None)
    loop_status[guild_id] = False

    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("👋 ออกจากห้องเสียงเรียบร้อยแล้ว")
    else:
        await interaction.response.send_message("❌ บอทไม่ได้อยู่ในห้องเสียง", ephemeral=True)

@client.tree.command(name="nowplaying", description="ดูข้อมูลเพลงที่กำลังเล่นอยู่ขณะนี้")
async def nowplaying(interaction: discord.Interaction):
    curr = now_playing.get(interaction.guild.id)
    if not curr:
        return await interaction.response.send_message("❌ ไม่มีเพลงที่กำลังเล่นอยู่ขณะนี้", ephemeral=True)

    embed = discord.Embed(
        title="🎧 กำลังเล่นเพลง",
        description=f"[{curr['title']}]({curr['webpage_url']})",
        color=discord.Color.purple()
    )
    embed.add_field(name="⏱️ ความยาว", value=format_sec(curr['duration']))
    embed.add_field(name="🔁 วนซ้ำ", value="เปิด" if loop_status.get(interaction.guild.id) else "ปิด")
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="loop", description="เปิดหรือปิดการวนซ้ำเพลงเดิม")
async def loop_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    status = not loop_status.get(guild_id, False)
    loop_status[guild_id] = status
    msg = "🔁 เปิดการวนซ้ำเพลงเดิมเรียบร้อย" if status else "➡️ ปิดการวนซ้ำเพลงแล้ว"
    await interaction.response.send_message(msg)

@client.tree.command(name="volume", description="ปรับระดับเสียงของบอท (0 - 100%)")
@app_commands.describe(level="ระดับเสียง 0 ถึง 100")
async def volume(interaction: discord.Interaction, level: int):
    if level < 0 or level > 100:
        return await interaction.response.send_message("❌ กรุณากำหนดระดับเสียงระหว่าง 0 ถึง 100", ephemeral=True)
    
    vol_float = level / 100.0
    volume_status[interaction.guild.id] = vol_float
    
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = vol_float

    await interaction.response.send_message(f"🔊 ปรับระดับเสียงเป็น **{level}%** เรียบร้อยแล้ว")

# =========================================================
# SELF ROLE COMMANDS
# =========================================================

@client.tree.command(name="setup_roles", description="สร้างแผงปุ่มรับยศแบบ Embed")
@app_commands.describe(
    title="หัวข้อของแผงรับยศ",
    description="คำอธิบายแผงรับยศ",
    image_url="URL ของรูปภาพหรือ GIF ประกอบ (ไม่ใส่ก็ได้)"
)
@app_commands.checks.has_permissions(manage_roles=True)
async def setup_roles(interaction: discord.Interaction, title: str, description: str, image_url: str = None):
    await interaction.response.defer()

    embed = discord.Embed(title=title, description=description, color=discord.Color.green())
    if image_url:
        embed.set_image(url=image_url)

    empty_view = discord.ui.View()
    msg = await interaction.followup.send(embed=embed, view=empty_view)

    db_execute(
        "INSERT INTO self_roles (message_id, guild_id, channel_id, title, description, image_url, roles_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg.id, interaction.guild.id, interaction.channel.id, title, description, image_url or "", "[]")
    )

@client.tree.command(name="add_role", description="เพิ่มยศลงในแผงรับยศที่มีอยู่")
@app_commands.describe(message_id="ID ของข้อความแผงรับยศ", role="ยศที่ต้องการเพิ่มลงแผง")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_role(interaction: discord.Interaction, message_id: str, role: discord.Role):
    await interaction.response.defer()
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.followup.send("❌ Message ID ไม่ถูกต้อง")

    row = db_fetch_one("SELECT title, description, image_url, roles_json, channel_id FROM self_roles WHERE message_id = ?", (msg_id,))
    if not row:
        return await interaction.followup.send("❌ ไม่พบแผงรับยศจาก Message ID นี้ในระบบ")

    title, description, image_url, roles_json, channel_id = row
    roles_data = json.loads(roles_json)

    if any(r[0] == str(role.id) for r in roles_data):
        return await interaction.followup.send("❌ ยศนี้มีอยู่ในแผงรับยศแล้ว")

    roles_data.append((str(role.id), role.name))
    new_roles_json = json.dumps(roles_data)

    db_execute("UPDATE self_roles SET roles_json = ? WHERE message_id = ?", (new_roles_json, msg_id))

    channel = interaction.guild.get_channel(channel_id)
    if channel:
        try:
            target_msg = await channel.fetch_message(msg_id)
            view = DynamicSelfRoleView(roles_data)
            await target_msg.edit(view=view)
            client.add_view(view)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ บันทึกข้อมูลแล้ว แต่ไม่สามารถแก้ไขข้อความแผงรับยศได้: {e}")

    await interaction.followup.send(f"✅ เพิ่มยศ **{role.name}** เข้าแผงรับยศเรียบร้อยแล้ว")

@client.tree.command(name="remove_role", description="ลบยศออกจากแผงรับยศ")
@app_commands.describe(message_id="ID ของข้อความแผงรับยศ", role="ยศที่ต้องการลบออกจากแผง")
@app_commands.checks.has_permissions(manage_roles=True)
async def remove_role(interaction: discord.Interaction, message_id: str, role: discord.Role):
    await interaction.response.defer()
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.followup.send("❌ Message ID ไม่ถูกต้อง")

    row = db_fetch_one("SELECT roles_json, channel_id FROM self_roles WHERE message_id = ?", (msg_id,))
    if not row:
        return await interaction.followup.send("❌ ไม่พบแผงรับยศจาก Message ID นี้ในระบบ")

    roles_json, channel_id = row
    roles_data = json.loads(roles_json)
    
    new_roles_data = [r for r in roles_data if r[0] != str(role.id)]
    if len(new_roles_data) == len(roles_data):
        return await interaction.followup.send("❌ ไม่พบยศนี้ในแผงรับยศ")

    new_roles_json = json.dumps(new_roles_data)
    db_execute("UPDATE self_roles SET roles_json = ? WHERE message_id = ?", (new_roles_json, msg_id))

    channel = interaction.guild.get_channel(channel_id)
    if channel:
        try:
            target_msg = await channel.fetch_message(msg_id)
            view = DynamicSelfRoleView(new_roles_data)
            await target_msg.edit(view=view)
        except Exception as e:
            pass

    await interaction.followup.send(f"✅ ลบยศ **{role.name}** ออกจากแผงรับยศเรียบร้อยแล้ว")

# =========================================================
# MODERATION COMMANDS
# =========================================================

@client.tree.command(name="ban", description="แบนสมาชิกออกจากเซิร์ฟเวอร์")
@app_commands.describe(member="สมาชิกที่ต้องการแบน", reason="เหตุผลในการแบน")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "ไม่ได้ระบุเหตุผล"):
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message("❌ คุณไม่สามารถแบนผู้ที่มียศสูงกว่าหรือเท่ากับคุณได้", ephemeral=True)
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 แบน {member.mention} เรียบร้อยแล้ว | เหตุผล: {reason}")

@client.tree.command(name="unban", description="ปลดแบนผู้ใช้ด้วย User ID")
@app_commands.describe(user_id="ID ของผู้ใช้ที่ต้องการปลดแบน")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        uid = int(user_id)
        user = await client.fetch_user(uid)
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"🔓 ปลดแบน **{user}** เรียบร้อยแล้ว")
    except Exception as e:
        await interaction.response.send_message(f"❌ ไม่สามารถปลดแบนได้: {e}", ephemeral=True)

@client.tree.command(name="kick", description="เตะสมาชิกออกจากเซิร์ฟเวอร์")
@app_commands.describe(member="สมาชิกที่ต้องการเตะ", reason="เหตุผลในการเตะ")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "ไม่ได้ระบุเหตุผล"):
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message("❌ คุณไม่สามารถเตะผู้ที่มียศสูงกว่าหรือเท่ากับคุณได้", ephemeral=True)
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 เตะ {member.mention} เรียบร้อยแล้ว | เหตุผล: {reason}")

@client.tree.command(name="timeout", description="ระงับการพิมพ์/ส่งข้อความชั่วคราว (Timeout)")
@app_commands.describe(member="สมาชิกที่ต้องการ Timeout", minutes="จำนวนนาทีที่ต้องการระงับ", reason="เหตุผล")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "ไม่ได้ระบุเหตุผล"):
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await interaction.response.send_message(f"⏳ Timeout {member.mention} เป็นเวลา {minutes} นาที | เหตุผล: {reason}")

@client.tree.command(name="untimeout", description="ยกเลิก Timeout สมาชิก")
@app_commands.describe(member="สมาชิกที่ต้องการยกเลิก Timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(f"🔊 ยกเลิก Timeout {member.mention} เรียบร้อยแล้ว")

@client.tree.command(name="warn", description="ตักเตือนสมาชิกและบันทึกลงระบบ")
@app_commands.describe(member="สมาชิกที่ต้องการเตือน", reason="เหตุผลในการเตือน")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_execute(
        "INSERT INTO warnings (guild_id, user_id, reason, moderator_id, timestamp) VALUES (?, ?, ?, ?, ?)",
        (interaction.guild.id, member.id, reason, interaction.user.id, now)
    )
    await interaction.response.send_message(f"⚠️ เตือน {member.mention} เรียบร้อยแล้ว | เหตุผล: {reason}")

@client.tree.command(name="warnings", description="ดูประวัติการตักเตือนของสมาชิก")
@app_commands.describe(member="สมาชิกที่ต้องการดูประวัติ")
async def warnings(interaction: discord.Interaction, member: discord.Member):
    rows = db_fetch_all("SELECT id, reason, timestamp FROM warnings WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id))
    if not rows:
        return await interaction.response.send_message(f"✅ {member.mention} ไม่มีประวัติการเตือน", ephemeral=True)

    desc = "\n".join([f"`ID: {r[0]}` - {r[1]} *(เมื่อ {r[2]})*" for r in rows])
    embed = discord.Embed(title=f"⚠️ ประวัติการเตือนของ {member.display_name}", description=desc, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="clearwarn", description="ล้างประวัติการตักเตือนของสมาชิก")
@app_commands.describe(member="สมาชิกที่ต้องการล้างประวัติเตือน")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarn(interaction: discord.Interaction, member: discord.Member):
    db_execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id))
    await interaction.response.send_message(f"🧹 ล้างประวัติการเตือนของ {member.mention} เรียบร้อยแล้ว")

@client.tree.command(name="purge", description="ลบข้อความจำนวนมาก")
@app_commands.describe(amount="จำนวนข้อความที่ต้องการลบ (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        return await interaction.response.send_message("❌ กรุณาระบุจำนวน 1 ถึง 100 ข้อความ", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 ลบข้อความแล้วจำนวน {len(deleted)} ข้อความ", ephemeral=True)

# =========================================================
# MANAGEMENT / ANNOUNCE COMMAND
# =========================================================

@client.tree.command(name="announce", description="ส่งข้อความประกาศในรูปแบบ Embed")
@app_commands.describe(channel="ช่องที่ต้องการส่งประกาศ", message="ข้อความประกาศ", title="หัวข้อประกาศ")
@app_commands.checks.has_permissions(administrator=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str, title: str = "📢 ประกาศสำคัญ"):
    embed = discord.Embed(title=title, description=message, color=discord.Color.blue())
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ ส่งประกาศไปที่ {channel.mention} เรียบร้อยแล้ว", ephemeral=True)

# =========================================================
# MEMBER EVENTS & CONFIGURATION COMMANDS
# =========================================================

def get_or_create_config(guild_id: int):
    row = db_fetch_one("SELECT * FROM server_configs WHERE guild_id = ?", (guild_id,))
    if not row:
        db_execute("INSERT INTO server_configs (guild_id) VALUES (?)", (guild_id,))

@client.tree.command(name="setwelcome", description="ตั้งค่าระบบข้อความต้อนรับสมาชิกใหม่")
@app_commands.describe(
    channel="ช่องสำหรับส่งข้อความต้อนรับ",
    message="ข้อความ (ใช้ {user}, {username}, {server}, {member_count} ได้)",
    image_url="URL รูปภาพหรือ GIF ประกอบ (ไม่ใส่ก็ได้)",
    enabled="เปิดหรือปิดระบบต้อนรับ"
)
@app_commands.checks.has_permissions(administrator=True)
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str, enabled: bool, image_url: str = None):
    get_or_create_config(interaction.guild.id)
    db_execute(
        "UPDATE server_configs SET welcome_channel_id = ?, welcome_msg = ?, welcome_img = ?, welcome_enabled = ? WHERE guild_id = ?",
        (channel.id, message, image_url or "", 1 if enabled else 0, interaction.guild.id)
    )
    await interaction.response.send_message(f"✅ ตั้งค่าระบบ Welcome ไปยังช่อง {channel.mention} (สถานะ: {'เปิด' if enabled else 'ปิด'}) เรียบร้อยแล้ว")

@client.tree.command(name="setgoodbye", description="ตั้งค่าระบบข้อความแจ้งสมาชิกออกจากเซิร์ฟเวอร์")
@app_commands.describe(
    channel="ช่องสำหรับส่งข้อความอำลา",
    message="ข้อความ (ใช้ {user}, {username}, {server}, {member_count} ได้)",
    image_url="URL รูปภาพหรือ GIF ประกอบ (ไม่ใส่ก็ได้)",
    enabled="เปิดหรือปิดระบบอำลา"
)
@app_commands.checks.has_permissions(administrator=True)
async def setgoodbye(interaction: discord.Interaction, channel: discord.TextChannel, message: str, enabled: bool, image_url: str = None):
    get_or_create_config(interaction.guild.id)
    db_execute(
        "UPDATE server_configs SET goodbye_channel_id = ?, goodbye_msg = ?, goodbye_img = ?, goodbye_enabled = ? WHERE guild_id = ?",
        (channel.id, message, image_url or "", 1 if enabled else 0, interaction.guild.id)
    )
    await interaction.response.send_message(f"✅ ตั้งค่าระบบ Goodbye ไปยังช่อง {channel.mention} (สถานะ: {'เปิด' if enabled else 'ปิด'}) เรียบร้อยแล้ว")

@client.tree.command(name="setboots", description="ตั้งค่าระบบแจ้งเตือนเมื่อมีคน Boost เซิร์ฟเวอร์")
@app_commands.describe(
    channel="ช่องสำหรับแจ้งเตือน Boost",
    message="ข้อความ (ใช้ {user}, {username}, {server} ได้)",
    image_url="URL รูปภาพหรือ GIF ประกอบ (ไม่ใส่ก็ได้)",
    enabled="เปิดหรือปิดระบบแจ้งเตือน Boost"
)
@app_commands.checks.has_permissions(administrator=True)
async def setboots(interaction: discord.Interaction, channel: discord.TextChannel, message: str, enabled: bool, image_url: str = None):
    get_or_create_config(interaction.guild.id)
    db_execute(
        "UPDATE server_configs SET boost_channel_id = ?, boost_msg = ?, boost_img = ?, boost_enabled = ? WHERE guild_id = ?",
        (channel.id, message, image_url or "", 1 if enabled else 0, interaction.guild.id)
    )
    await interaction.response.send_message(f"✅ ตั้งค่าระบบ Boost Alert ไปยังช่อง {channel.mention} (สถานะ: {'เปิด' if enabled else 'ปิด'}) เรียบร้อยแล้ว")

# Listeners for Member Join/Leave/Update (Boost)
@client.event
async def on_member_join(member: discord.Member):
    row = db_fetch_one("SELECT welcome_channel_id, welcome_msg, welcome_img, welcome_enabled FROM server_configs WHERE guild_id = ?", (member.guild.id,))
    if row and row[3] == 1 and row[0]:
        channel = member.guild.get_channel(row[0])
        if channel:
            msg_text = row[1] or "ยินดีต้อนรับ {user} สู่ {server}!"
            msg_text = msg_text.format(user=member.mention, username=member.name, server=member.guild.name, member_count=member.guild.member_count)
            embed = discord.Embed(title="🎉 ยินดีต้อนรับสมาชิกใหม่!", description=msg_text, color=discord.Color.green())
            if row[2]:
                embed.set_image(url=row[2])
            await channel.send(embed=embed)

@client.event
async def on_member_remove(member: discord.Member):
    row = db_fetch_one("SELECT goodbye_channel_id, goodbye_msg, goodbye_img, goodbye_enabled FROM server_configs WHERE guild_id = ?", (member.guild.id,))
    if row and row[3] == 1 and row[0]:
        channel = member.guild.get_channel(row[0])
        if channel:
            msg_text = row[1] or "{username} ได้ออกจาก {server} แล้ว"
            msg_text = msg_text.format(user=member.mention, username=member.name, server=member.guild.name, member_count=member.guild.member_count)
            embed = discord.Embed(title="👋 สมาชิกออกจากเซิร์ฟเวอร์", description=msg_text, color=discord.Color.red())
            if row[2]:
                embed.set_image(url=row[2])
            await channel.send(embed=embed)

@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Detect Server Boost Event
    if before.premium_since is None and after.premium_since is not None:
        row = db_fetch_one("SELECT boost_channel_id, boost_msg, boost_img, boost_enabled FROM server_configs WHERE guild_id = ?", (after.guild.id,))
        if row and row[3] == 1 and row[0]:
            channel = after.guild.get_channel(row[0])
            if channel:
                msg_text = row[1] or "ขอบคุณ {user} ที่กด Boost ให้กับ {server}! 🚀"
                msg_text = msg_text.format(user=after.mention, username=after.name, server=after.guild.name)
                embed = discord.Embed(title="💎 มีการ Boost เซิร์ฟเวอร์!", description=msg_text, color=discord.Color.magenta())
                if row[2]:
                    embed.set_image(url=row[2])
                await channel.send(embed=embed)

# =========================================================
# MAIN ENTRY POINT
# =========================================================

if __name__ == "__main__":
    keep_alive()
    if TOKEN:
        client.run(TOKEN)
    else:
        print("❌ [ERROR] ไม่พบ DISCORD_TOKEN ใน Environment Variables")
