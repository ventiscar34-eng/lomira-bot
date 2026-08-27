import os
import asyncio
from flask import Flask
from threading import Thread
import discord
from discord import app_commands

# ==========================================
# 1. ระบบ Web Server (Flask) สำหรับ Render
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "LOMIRA Bot is Alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ==========================================
# 2. ตั้งค่า Discord Bot Client
# ==========================================
intents = discord.Intents.default()
intents.message_content = True

class LomiraBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # ฟังก์ชันเคลียร์และ Sync คำสั่ง Slash Command ใหม่ทั้งหมด
        print("[LOMIRA] Clearing old commands...")
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        print("[LOMIRA] Synced new commands successfully!")

client = LomiraBot()

@client.event
async def on_ready():
    print(f"[LOMIRA] Online as {client.user}")

# ==========================================
# 3. ตัวอย่างคำสั่ง Slash Commands (ใช้ defer กันหมดเวลา)
# ==========================================

@client.tree.command(name="ping", description="เช็กสถานะการตอบสนองของบอท")
async def ping(interaction: discord.Interaction):
    # 1. สั่ง defer() ทันทีเพื่อขอเวลาเพิ่ม (แก้ปัญหาแอปไม่ตอบสนอง)
    await interaction.response.defer()
    
    # 2. ทำงานประมวลผล
    latency = round(client.latency * 1000)
    
    # 3. ตอบกลับด้วย followup.send
    await interaction.followup.send(f"🏓 พิง! ความเร็วตอบสนอง: {latency} ms")

@client.tree.command(name="play", description="สั่งเล่นเพลง")
@app_commands.describe(query="ชื่อเพลง หรือ ลิงก์เพลง")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    
    # ใส่ระบบค้นหา/เล่นเพลงของคุณตรงนี้
    await interaction.followup.send(f"🎵 กำลังค้นหาและเตรียมเล่น: **{query}**")

@client.tree.command(name="stop", description="หยุดเล่นเพลงและออกจากช่องเสียง")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    
    await interaction.followup.send("⏹️ หยุดเล่นเพลงเรียบร้อยแล้ว")

# ==========================================
# 4. เริ่มการทำงานของ Bot & Server
# ==========================================
keep_alive()

token = os.getenv('DISCORD_TOKEN')
if token:
    client.run(token)
else:
    print("[ERROR] ไม่พบ DISCORD_TOKEN ใน Environment Variables!")
