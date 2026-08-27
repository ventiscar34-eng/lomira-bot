import os
import discord
from flask import Flask
from threading import Thread

# --- 1. โค้ดหลอก Port สำหรับ Render ---
app = Flask('')


@app.route('/')
def home():
  return "Bot is alive!"


def run():
  app.run(host='0.0.0.0', port=8080)


def keep_alive():
  t = Thread(target=run)
  t.start()


# --- 2. โค้ดบอท Discord ---
keep_alive()  # เรียกใช้งานเว็บ

client = discord.Client(intents=discord.Intents.default())


@client.event
async def on_ready():
  print(f'Logged in as {client.user}')


token = os.getenv('DISCORD_TOKEN')
client.run(token)
