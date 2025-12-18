import vacefron
import time
import random
import sqlite3
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv # type: ignore
import pytz # type: ignore
from datetime import datetime
import asyncio

# --- global toggles (default: off) ---
MESSAGE_LOGGING_ENABLED = False

# Setting discord bot intents
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='/', intents=intents)

# Loading .env
load_dotenv()

# DB name
DATABASE = "levels.sqlite"

# Database connection thang
database = sqlite3.connect(DATABASE)
cursor = database.cursor()

# Makes users db if not there (GLOBAL)
cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    user_name TEXT,
    level INTEGER,
    xp INTEGER,
    levelup_xp INTEGER
)""")

# Cooldown table: stores last exp time per user per guild
cursor.execute("""CREATE TABLE IF NOT EXISTS exp_cooldowns (
    guild_id INTEGER,
    user_id INTEGER,
    last_exp_time REAL,
    PRIMARY KEY (guild_id, user_id)
)""")

# Message log table
cursor.execute("""CREATE TABLE IF NOT EXISTS message_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    message_link TEXT,
    message_content TEXT,
    channel_id INTEGER,
    channel_link TEXT,
    message_author_name TEXT,
    message_author_id INTEGER,
    date_and_time_sent TEXT,
    guild_id INTEGER,
    guild_invite_link TEXT
)""")

# Level Up Channel Table
cursor.execute("""CREATE TABLE IF NOT EXISTS level_up_channels (
    guild_id INTEGER,
    channel_id INTEGER,
    PRIMARY KEY (guild_id, channel_id)
)""")
database.commit()

# Cache for permanent invites {guild_id: invite_url}
guild_invite_cache = {}

# Rotating status task
async def rotate_status():
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Status 1: Number of servers
        server_count = len(bot.guilds)
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{server_count} servers"))
        await asyncio.sleep(20)
        # Status 2: Number of unique users tracked in the database
        try:
            conn = sqlite3.connect(DATABASE)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT user_id) FROM users")
            user_count = cur.fetchone()[0]
            conn.close()
        except Exception:
            user_count = "?"
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{user_count} users"))
        await asyncio.sleep(20)

# Event for when bot starts up
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}, have fun with the leveling :)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s) with Discord.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    bot.loop.create_task(rotate_status())

# Gets level up channels for a guild
def get_level_up_channels(conn, guild_id):
    cur = conn.cursor()
    cur.execute("SELECT channel_id FROM level_up_channels WHERE guild_id=?", (guild_id,))
    return [row[0] for row in cur.fetchall()]

# Adds a channel id to the level_up_channels table
def add_level_up_channel(conn, guild_id, channel_id):
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO level_up_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Channel already exists
        return False

# Gets user (GLOBAL)
def get_user(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT level, xp, levelup_xp FROM users WHERE user_id=?", (user_id,))
    return cur.fetchone()

# Adds or updates users (GLOBAL)
def add_or_update_user(conn, user_id, user_name, level, xp, levelup_xp):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (user_id, user_name, level, xp, levelup_xp) VALUES (?, ?, ?, ?, ?)",
        (user_id, user_name, level, xp, levelup_xp)
    )
    conn.commit()

# Gets last time the user got granted exp (per guild cooldown)
def get_last_exp_time(conn, guild_id, user_id):
    cur = conn.cursor()
    cur.execute("SELECT last_exp_time FROM exp_cooldowns WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    return row[0] if row else None

# Sets the time that the user got their last amount of exp (per guild cooldown)
def set_last_exp_time(conn, guild_id, user_id, timestamp):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO exp_cooldowns (guild_id, user_id, last_exp_time) VALUES (?, ?, ?)",
        (guild_id, user_id, timestamp)
    )
    conn.commit()

def get_message_content_with_attachments(message):
    content = message.content
    # Add URLs of attachments (images/videos)
    if message.attachments:
        content += " " + " ".join([a.url for a in message.attachments])
    return content.strip()

# Get or create a permanent invite for logging
async def get_or_create_permanent_invite(guild):
    if guild.id in guild_invite_cache:
        return guild_invite_cache[guild.id]
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).create_instant_invite:
            try:
                invite = await channel.create_invite(max_age=0, max_uses=0, unique=False)
                guild_invite_cache[guild.id] = invite.url
                return invite.url
            except Exception as e:
                print(f"Could not create invite for guild {guild.id}: {e}")
                continue
    return None

# Message logging function
async def log_message_to_db(message):
    conn = sqlite3.connect(DATABASE)
    wa_tz = pytz.timezone('Australia/Perth')
    dt_wa = message.created_at.astimezone(wa_tz)
    message_content = get_message_content_with_attachments(message)
    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
    channel_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}"

    guild_invite_link = await get_or_create_permanent_invite(message.guild)
    if guild_invite_link is None:
        guild_invite_link = "No invite available"

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO message_logs (
            message_id, message_link, message_content, channel_id, channel_link,
            message_author_name, message_author_id, date_and_time_sent,
            guild_id, guild_invite_link
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.id,
        message_link,
        message_content,
        message.channel.id,
        channel_link,
        str(message.author),
        message.author.id,
        dt_wa.strftime('%Y-%m-%d %H:%M:%S'),
        message.guild.id,
        guild_invite_link
    ))
    conn.commit()
    conn.close()

# Main message event
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

        # Log the message only if enabled
    if MESSAGE_LOGGING_ENABLED and not message.author.bot or not message.guild:
        await log_message_to_db(message)

        guild_id = message.guild.id
        user_id = message.author.id
        user_name = str(message.author)

        conn = sqlite3.connect(DATABASE)
        # Check cooldown (per guild)
        last_exp_time = get_last_exp_time(conn, guild_id, user_id)
        now = time.time()
        can_give_exp = False
        if last_exp_time is None or now - last_exp_time >= 10:
            can_give_exp = True
            set_last_exp_time(conn, guild_id, user_id, now)

        if can_give_exp:
            exp_give = random.randint(1, 20)
            user = get_user(conn, user_id)

            if user is None:
                level, xp, levelup_xp = 1, 10, 100  # Starting values
            else:
                level, xp, levelup_xp = user
                xp += exp_give  # XP per message

            leveled_up = False
            if xp >= levelup_xp:
                level += 1
                xp = xp - levelup_xp
                levelup_xp = int(50 * level ** 2 + 100 * level + 50)  # Example formula
                leveled_up = True

            add_or_update_user(conn, user_id, user_name, level, xp, levelup_xp)

            if leveled_up:
                # Notify all level-up channels in all guilds
                conn2 = sqlite3.connect(DATABASE)
                cur = conn2.cursor()
                cur.execute("SELECT guild_id, channel_id FROM level_up_channels")
                for guild_id2, channel_id in cur.fetchall():
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            await channel.send(f"<@{user_id}> leveled up to level {level}! 🎉")
                        except Exception as e:
                            print(f"Failed to send level up message in guild {guild_id2}, channel {channel_id}: {e}")
                conn2.close()
        conn.close()

        await bot.process_commands(message)

    elif MESSAGE_LOGGING_ENABLED == False:
        return

# Leaderboard command (top 25 by EXP)
@bot.tree.command(name="leaderboard", description="Shows the top 25 users by Level.")
async def leaderboard(interaction: discord.Interaction):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT user_name, level FROM users ORDER BY level DESC LIMIT 25")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No users found in the leaderboard.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🏆 Top 25 Leaderboard",
        description="Here are the top 25 users by Level!",
        color=discord.Color.gold()
    )

    leaderboard_text = ""
    for idx, (user_name, level) in enumerate(rows, start=1):
        leaderboard_text += f"**{idx}.** `{user_name}` | **Level:** {level}\n"

    embed.add_field(name="Rankings", value=leaderboard_text, inline=False)
    await interaction.response.send_message(embed=embed)

# Sends the main discord server invite -- DO NOT TOUCH
@bot.tree.command(name="invite", description="Sends the Null Studios discord server invite.")
async def invite(interaction: discord.Interaction):
    await interaction.response.send_message("https://discord.gg/Km6wxApqbm", ephemeral=True)

@bot.tree.command(name='add_levelup_channel', description="Adds a channel to show when users level up. Used once per user.")
async def add_levelup_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    # Check if user is guild owner
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the server owner can add level-up channels.", ephemeral=True)
        return

    # Check if bot can access the channel
    permissions = channel.permissions_for(interaction.guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        await interaction.response.send_message("I don't have permission to access that channel.", ephemeral=True)
        return

    conn = sqlite3.connect(DATABASE)
    existing_channels = get_level_up_channels(conn, interaction.guild.id)
    if channel.id in existing_channels:
        await interaction.response.send_message("This channel is already set as a level-up channel.", ephemeral=True)
        conn.close()
        return

    # Add channel
    success = add_level_up_channel(conn, interaction.guild.id, channel.id)
    conn.close()

    if success:
        await interaction.response.send_message(f"Channel {channel.mention} has been added as a level-up channel.", ephemeral=True)
    else:
        await interaction.response.send_message("Failed to add the channel. It might already be added.", ephemeral=True)

@bot.tree.command(name="sync", description="Syncs the bot's commands with Discord.")
async def sync_commands(interaction: discord.Interaction):
    sender_id = interaction.user.id
    if sender_id == 769912339255263233: #os.getenv("MASTER_USER_ID")
        try:
            synced = await bot.tree.sync()
            await interaction.response.send_message(f"Synced {len(synced)} command(s) with Discord.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to sync commands: {e}")
    else:
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

#Sends the bot invite
@bot.tree.command(name="bot_invite", description="Sends the CSL bot link.")
async def bot_invite(interaction: discord.Interaction):
    await interaction.response.send_message("https://discord.com/oauth2/authorize?client_id=1378764772685779055", ephemeral=True)

bot.run(os.getenv("TOKEN"))
