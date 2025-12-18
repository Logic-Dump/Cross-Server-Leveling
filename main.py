import vacefron
import time
import random
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv  # type: ignore
import pytz  # type: ignore
from datetime import datetime
import asyncio

from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

# --- global toggles (default: off) ---
MESSAGE_LOGGING_ENABLED = False

# Loading .env
load_dotenv()

# Discord bot intents + sharding
intents = discord.Intents.all()
bot = commands.AutoShardedBot(
    command_prefix='/',
    intents=intents,
    shard_count=int((os.getenv("SHARD_AMOUNT") or 1))
)

# ------------------------------------------------------------------
# DATABASE: SQLAlchemy over SQLite file (levels.sqlite)
# ------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///levels.sqlite"

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    user_id = Column(BigInteger, primary_key=True)
    user_name = Column(Text)
    level = Column(Integer)
    xp = Column(Integer)
    levelup_xp = Column(Integer)


class ExpCooldown(Base):
    __tablename__ = "exp_cooldowns"

    guild_id = Column(BigInteger, primary_key=True)
    user_id = Column(BigInteger, primary_key=True)
    last_exp_time = Column(Float)


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger)
    message_link = Column(Text)
    message_content = Column(Text)
    channel_id = Column(BigInteger)
    channel_link = Column(Text)
    message_author_name = Column(Text)
    message_author_id = Column(BigInteger)
    date_and_time_sent = Column(Text)
    guild_id = Column(BigInteger)
    guild_invite_link = Column(Text)


class LevelUpChannel(Base):
    __tablename__ = "level_up_channels"

    guild_id = Column(BigInteger, primary_key=True)
    channel_id = Column(BigInteger, primary_key=True)


# Create tables
Base.metadata.create_all(bind=engine)

# Cache for permanent invites {guild_id: invite_url}
guild_invite_cache = {}


# ------------------------------------------------------------------
# DB helper functions
# ------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        return db
    finally:
        # Don't close here, caller closes in finally
        pass


def get_message_count():
    db = get_db()
    try:
        count = db.query(MessageLog).count()
        return count
    except Exception:
        return "?"
    finally:
        db.close()


def get_level_up_channels(guild_id: int):
    db = get_db()
    try:
        channels = db.query(LevelUpChannel.channel_id).filter(
            LevelUpChannel.guild_id == guild_id
        ).all()
        return [c.channel_id for c in channels]
    finally:
        db.close()


def add_level_up_channel_db(guild_id: int, channel_id: int):
    db = get_db()
    try:
        new_channel = LevelUpChannel(guild_id=guild_id, channel_id=channel_id)
        db.add(new_channel)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def get_user_db(user_id: int):
    db = get_db()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            return (user.level, user.xp, user.levelup_xp)
        return None
    finally:
        db.close()


def add_or_update_user_db(user_id: int, user_name: str, level: int, xp: int, levelup_xp: int):
    db = get_db()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            user.user_name = user_name
            user.level = level
            user.xp = xp
            user.levelup_xp = levelup_xp
        else:
            user = User(
                user_id=user_id,
                user_name=user_name,
                level=level,
                xp=xp,
                levelup_xp=levelup_xp
            )
            db.add(user)
        db.commit()
    finally:
        db.close()


def get_last_exp_time_db(guild_id: int, user_id: int):
    db = get_db()
    try:
        cooldown = db.query(ExpCooldown).filter(
            ExpCooldown.guild_id == guild_id,
            ExpCooldown.user_id == user_id
        ).first()
        return cooldown.last_exp_time if cooldown else None
    finally:
        db.close()


def set_last_exp_time_db(guild_id: int, user_id: int, timestamp: float):
    db = get_db()
    try:
        cooldown = db.query(ExpCooldown).filter(
            ExpCooldown.guild_id == guild_id,
            ExpCooldown.user_id == user_id
        ).first()
        if cooldown:
            cooldown.last_exp_time = timestamp
        else:
            cooldown = ExpCooldown(
                guild_id=guild_id,
                user_id=user_id,
                last_exp_time=timestamp
            )
            db.add(cooldown)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------
# Utility functions (logging etc.)
# ------------------------------------------------------------------
def get_message_content_with_attachments(message: discord.Message) -> str:
    content = message.content
    if message.attachments:
        content += " " + " ".join([a.url for a in message.attachments])
    return content.strip()


async def get_or_create_permanent_invite(guild: discord.Guild):
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


async def log_message_to_db(message: discord.Message):
    wa_tz = pytz.timezone('Australia/Perth')
    dt_wa = message.created_at.astimezone(wa_tz)
    message_content = get_message_content_with_attachments(message)
    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
    channel_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}"

    guild_invite_link = await get_or_create_permanent_invite(message.guild)
    if guild_invite_link is None:
        guild_invite_link = "No invite available"

    db = get_db()
    try:
        message_log = MessageLog(
            message_id=message.id,
            message_link=message_link,
            message_content=message_content,
            channel_id=message.channel.id,
            channel_link=channel_link,
            message_author_name=str(message.author),
            message_author_id=message.author.id,
            date_and_time_sent=dt_wa.strftime('%Y-%m-%d %H:%M:%S'),
            guild_id=message.guild.id,
            guild_invite_link=guild_invite_link
        )
        db.add(message_log)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------
# Rotating status
# ------------------------------------------------------------------
async def rotate_status():
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Status 1: servers
        server_count = len(bot.guilds)
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{server_count} servers"
            )
        )
        await asyncio.sleep(20)

        # Status 2: unique users in db
        try:
            db = get_db()
            user_count = db.query(User.user_id).distinct().count()
            db.close()
        except Exception:
            user_count = "?"
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{user_count} users"
            )
        )
        await asyncio.sleep(20)


# ------------------------------------------------------------------
# Events
# ------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} with shard count of {bot.shard_count}, have fun with the leveling :)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s) with Discord.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    bot.loop.create_task(rotate_status())


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # Log the message only if enabled
    if MESSAGE_LOGGING_ENABLED:
        await log_message_to_db(message)

    guild_id = message.guild.id
    user_id = message.author.id
    user_name = str(message.author)

    # cooldown check
    last_exp_time = get_last_exp_time_db(guild_id, user_id)
    now = time.time()
    can_give_exp = False
    if last_exp_time is None or now - last_exp_time >= 10:
        can_give_exp = True
        set_last_exp_time_db(guild_id, user_id, now)

    if can_give_exp:
        exp_give = random.randint(1, 20)
        user = get_user_db(user_id)

        if user is None:
            level, xp, levelup_xp = 1, 10, 100
        else:
            level, xp, levelup_xp = user
            xp += exp_give

        leveled_up = False
        if xp >= levelup_xp:
            level += 1
            xp = xp - levelup_xp
            levelup_xp = int(50 * level ** 2 + 100 * level + 50)
            leveled_up = True

        add_or_update_user_db(user_id, user_name, level, xp, levelup_xp)

        if leveled_up:
            db = get_db()
            try:
                channels = db.query(LevelUpChannel).all()
                for channel_record in channels:
                    channel = bot.get_channel(channel_record.channel_id)
                    if channel:
                        try:
                            await channel.send(f"<@{user_id}> leveled up to level {level}! 🎉")
                        except Exception as e:
                            print(
                                f"Failed to send level up message in guild {channel_record.guild_id}, "
                                f"channel {channel_record.channel_id}: {e}"
                            )
            finally:
                db.close()

    await bot.process_commands(message)


# ------------------------------------------------------------------
# Slash commands
# ------------------------------------------------------------------
@bot.tree.command(name="leaderboard", description="Shows the top 25 users by Level.")
async def leaderboard(interaction: discord.Interaction):
    db = get_db()
    try:
        users = db.query(User.user_name, User.level).order_by(
            User.level.desc()
        ).limit(25).all()
    finally:
        db.close()

    if not users:
        await interaction.response.send_message(
            "No users found in the leaderboard.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🏆 Top 25 Leaderboard",
        description="Here are the top 25 users by Level!",
        color=discord.Color.gold()
    )

    leaderboard_text = ""
    for idx, (user_name, level) in enumerate(users, start=1):
        leaderboard_text += f"**{idx}.** `{user_name}` | **Level:** {level}\n"

    embed.add_field(name="Rankings", value=leaderboard_text, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="invite", description="Sends the Null Studios discord server invite.")
async def invite(interaction: discord.Interaction):
    await interaction.response.send_message(
        "https://discord.gg/Km6wxApqbm",
        ephemeral=True
    )


@bot.tree.command(
    name='add_levelup_channel',
    description="Adds a channel to show when users level up. Used once per user."
)
async def add_levelup_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    # Check if user is guild owner
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(
            "Only the server owner can add level-up channels.",
            ephemeral=True
        )
        return

    # Check if bot can access the channel
    permissions = channel.permissions_for(interaction.guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        await interaction.response.send_message(
            "I don't have permission to access that channel.",
            ephemeral=True
        )
        return

    existing_channels = get_level_up_channels(interaction.guild.id)
    if channel.id in existing_channels:
        await interaction.response.send_message(
            "This channel is already set as a level-up channel.",
            ephemeral=True
        )
        return

    success = add_level_up_channel_db(interaction.guild.id, channel.id)
    if success:
        await interaction.response.send_message(
            f"Channel {channel.mention} has been added as a level-up channel.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Failed to add the channel. It might already be added.",
            ephemeral=True
        )


@bot.tree.command(name="sync", description="Syncs the bot's commands with Discord.")
async def sync_commands(interaction: discord.Interaction):
    sender_id = interaction.user.id
    if sender_id == 769912339255263233:  # or int(os.getenv("MASTER_USER_ID") or 0)
        try:
            synced = await bot.tree.sync()
            await interaction.response.send_message(
                f"Synced {len(synced)} command(s) with Discord.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to sync commands: {e}",
                ephemeral=True
            )
    else:
        await interaction.response.send_message(
            "You are not authorized to use this command.",
            ephemeral=True
        )


@bot.tree.command(name="bot_invite", description="Sends the CSL bot link.")
async def bot_invite(interaction: discord.Interaction):
    await interaction.response.send_message(
        "https://discord.com/oauth2/authorize?client_id=1378764772685779055",
        ephemeral=True
    )


# ------------------------------------------------------------------
# Run bot
# ------------------------------------------------------------------
bot.run(os.getenv("TOKEN"))
