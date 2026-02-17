#!/usr/bin/env python3
"""
Discord <-> Stoat bidirectional bridge with Multi-Channel Support.

Requirements:
    pip install discord.py stoat.py python-dotenv asyncpg

Configuration:
    Fill in your tokens in .env.
    DATABASE_URL must be set in .env.
"""

import asyncio
import logging
import os
import sys

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv
import stoat

load_dotenv()

# ----------------------------------------------------------------------
#  CONFIGURATION
# ----------------------------------------------------------------------

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
STOAT_BOT_TOKEN   = os.getenv("STOAT_BOT_TOKEN")
DATABASE_URL      = os.getenv("DATABASE_URL")

# ----------------------------------------------------------------------
#  LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

# ----------------------------------------------------------------------
#  SHARED STATE
# ----------------------------------------------------------------------

db_pool: asyncpg.Pool | None = None
discord_bot = None  # Will be set in main
stoat_bot = None    # Will be set in main

# ----------------------------------------------------------------------
#  DATABASE FUNCTIONS
# ----------------------------------------------------------------------

async def init_db():
    global db_pool
    if not DATABASE_URL:
        logger.critical("DATABASE_URL is not set!")
        sys.exit(1)
    
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Database connection established.")
    except Exception as e:
        logger.critical(f"Failed to connect to database: {e}")
        sys.exit(1)

    # Create tables
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bridges (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                bridge_id INTEGER REFERENCES bridges(id) ON DELETE CASCADE,
                platform TEXT NOT NULL CHECK (platform IN ('discord', 'stoat')),
                channel_id TEXT NOT NULL,
                UNIQUE (platform, channel_id)
            );
        """)

async def create_bridge(platform: str, channel_id: str) -> int:
    async with db_pool.acquire() as conn:
        # Check if channel is already in a bridge
        existing = await conn.fetchval(
            "SELECT bridge_id FROM channels WHERE platform = $1 AND channel_id = $2",
            platform, str(channel_id) # Ensure channel_id is string
        )
        if existing:
            raise ValueError(f"Channel is already in bridge {existing}")

        # Create new bridge
        bridge_id = await conn.fetchval("INSERT INTO bridges DEFAULT VALUES RETURNING id")
        # Add channel to bridge
        await conn.execute(
            "INSERT INTO channels (bridge_id, platform, channel_id) VALUES ($1, $2, $3)",
            bridge_id, platform, str(channel_id)
        )
        return bridge_id

async def join_bridge(bridge_id: int, platform: str, channel_id: str):
    async with db_pool.acquire() as conn:
        # Check bridge exists
        exists = await conn.fetchval("SELECT id FROM bridges WHERE id = $1", bridge_id)
        if not exists:
            raise ValueError(f"Bridge {bridge_id} does not exist.")
            
        # Check if channel is already in a bridge
        existing = await conn.fetchval(
            "SELECT bridge_id FROM channels WHERE platform = $1 AND channel_id = $2",
            platform, str(channel_id)
        )
        if existing:
             if existing == bridge_id:
                 return # Already in this bridge
             raise ValueError(f"Channel is already in bridge {existing}")

        await conn.execute(
            "INSERT INTO channels (bridge_id, platform, channel_id) VALUES ($1, $2, $3)",
            bridge_id, platform, str(channel_id)
        )

async def leave_bridge(platform: str, channel_id: str):
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM channels WHERE platform = $1 AND channel_id = $2",
            platform, str(channel_id)
        )
        if result == "DELETE 0":
             raise ValueError("Channel is not in any bridge.")

async def get_bridge_status(platform: str, channel_id: str) -> tuple[int, list]:
    async with db_pool.acquire() as conn:
        bridge_id = await conn.fetchval(
            "SELECT bridge_id FROM channels WHERE platform = $1 AND channel_id = $2",
             platform, str(channel_id)
        )
        if not bridge_id:
            return None, []
        
        channels = await conn.fetch(
            "SELECT platform, channel_id FROM channels WHERE bridge_id = $1",
            bridge_id
        )
        return bridge_id, channels

async def get_bridge_destinations(sender_platform: str, sender_channel_id: str) -> list:
    """Returns a list of (platform, channel_id) to forward messages to."""
    if not db_pool: return []
    
    async with db_pool.acquire() as conn:
        bridge_id = await conn.fetchval(
            "SELECT bridge_id FROM channels WHERE platform = $1 AND channel_id = $2",
            sender_platform, str(sender_channel_id)
        )
        if not bridge_id:
            return []
        
        # Get all OTHER channels in the bridge
        rows = await conn.fetch(
            """
            SELECT platform, channel_id 
            FROM channels 
            WHERE bridge_id = $1 
            AND NOT (platform = $2 AND channel_id = $3)
            """,
            bridge_id, sender_platform, str(sender_channel_id)
        )
        return rows # list of Records

# ----------------------------------------------------------------------
#  STOAT BOT
# ----------------------------------------------------------------------

class StoatBot(stoat.Client):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pong_event = asyncio.Event()
        self.keep_alive_task = None

    async def on_ready(self, event, /):
        logger.info(f"Stoat: connected as {self.me}")
        if not self.keep_alive_task:
            self.keep_alive_task = self.loop.create_task(self.keep_alive_loop())
            logger.info("Stoat: Keepalive task started.")

    async def on_socket_response(self, msg):
        # Listen for PONG
        if isinstance(msg, dict) and msg.get("op") == "PONG":
            logger.debug("Stoat: PONG received.")
            self.pong_event.set()

    async def keep_alive_loop(self):
        logger.info("Stoat: Starting manual keepalive loop...")
        while not self.is_closed():
            try:
                # 1. Send PING
                # Assuming self.ws is exposed and has send_json
                if hasattr(self, 'ws') and self.ws:
                    logger.debug("Stoat: Sending PING...")
                    await self.ws.send_json({"op": "PING"})
                else:
                    logger.warning("Stoat: self.ws not available for PING.")
                    await asyncio.sleep(5)
                    continue

                # 2. Wait for PONG
                self.pong_event.clear()
                try:
                    await asyncio.wait_for(self.pong_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.error("Stoat: Ping Timeout (Zombie Connection)! Reconnecting...")
                    # 4. Force Reconnect
                    await self.close()
                    # self.close() should trigger the main loop to exit or reconnect depending on lib
                    break
            
                # 3. Sleep before next ping
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stoat Keepalive Error: {e}")
                await asyncio.sleep(5)

    async def on_message_create(self, event: stoat.MessageCreateEvent, /):
        msg = event.message
        if msg.author_id == self.me.id:
            return
        
        # Command Handling: !dbridger bridge ...
        if msg.content and msg.content.startswith("!dbridger bridge"):
            await self.handle_bridge_command(msg)
            return

        # Forwarding Logic
        if msg.content:
            dests = await get_bridge_destinations("stoat", str(msg.channel_id))
            if not dests:
                return 

            author_name = msg.author.display_name or msg.author.name
            avatar_url = msg.author.avatar.url() if msg.author.avatar else None

            for row in dests:
                dest_platform = row['platform']
                dest_channel_id = row['channel_id']

                if dest_platform == 'discord':
                    await self.send_to_discord(dest_channel_id, msg.content, author_name, avatar_url)

    async def send_to_discord(self, channel_id, content, username, avatar_url):
        if not discord_bot: return
        try:
            channel = discord_bot.get_channel(int(channel_id)) or await discord_bot.fetch_channel(int(channel_id))
            if not channel:
                logger.error(f"Stoat -> Discord: Channel {channel_id} not found")
                return
            
            # Find or create webhook
            webhook = None
            for wh in await channel.webhooks():
                if wh.user == discord_bot.user:
                    webhook = wh
                    break
            if not webhook:
                webhook = await channel.create_webhook(name="Stoat Bridge")

            await webhook.send(
                content=content[:2000],
                username=username[:80],
                avatar_url=avatar_url,
                wait=True
            )
            logger.info(f"Stoat -> Discord (Ch: {channel_id}): sent message from {username}")

        except Exception as e:
            logger.error(f"Stoat -> Discord: Failed to send to {channel_id} - {e}")

    async def handle_bridge_command(self, msg):
        # Syntax: !dbridger bridge [create|join|leave|status] [args]
        parts = msg.content.strip().split()
        if len(parts) < 3:
            return # Invalid command
        
        subcommand = parts[2].lower()
        channel_id = str(msg.channel_id)

        try:
            if subcommand == "create":
                bridge_id = await create_bridge("stoat", channel_id)
                await msg.reply(f"Bridge created! ID: **{bridge_id}**. Use `!dbridger bridge join {bridge_id}` in other channels.")
            
            elif subcommand == "join":
                if len(parts) < 4:
                    await msg.reply("Usage: `!dbridger bridge join <id>`")
                    return
                try:
                    bridge_id = int(parts[3])
                except ValueError:
                    await msg.reply("Invalid Bridge ID (must be a number).")
                    return
                
                await join_bridge(bridge_id, "stoat", channel_id)
                await msg.reply(f"Joined bridge {bridge_id}!")

            elif subcommand == "leave":
                await leave_bridge("stoat", channel_id)
                await msg.reply("Left the bridge.")

            elif subcommand == "status":
                bridge_id, channels = await get_bridge_status("stoat", channel_id)
                if not bridge_id:
                    await msg.reply("This channel is not in a bridge.")
                else:
                    text = f"Bridge ID: **{bridge_id}**\nConnected Channels:\n"
                    for r in channels:
                        text += f"- {r['platform']}: {r['channel_id']}\n"
                    await msg.reply(text)

        except ValueError as e:
            await msg.reply(f"Error: {str(e)}")
        except Exception as e:
            logger.error(f"Stoat Command Error: {e}")
            await msg.reply("An internal error occurred.")


# ----------------------------------------------------------------------
#  DISCORD BOT
# ----------------------------------------------------------------------

class DiscordBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.webhooks = True
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        logger.info(f"Discord: connected as {self.user}")

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id:
            return

        # Let commands process first (for !dbridger)
        await self.process_commands(message)

        # Forwarding Logic is ONLY for non-command messages usually?
        # But we check if it is a command. 
        # Actually, process_commands does not stop execution if no command found, but !dbridger is a command.
        # So we should check if it was a command or just chat. 
        # Simple check: if it starts with command prefix, ignore? 
        # But prefix is "!", normal messages might start with ! if not a command.
        # We can just check `message.content.startswith("!dbridger")` manually to be safe or rely on context.
        
        if message.content.startswith("!dbridger"):
            return

        dests = await get_bridge_destinations("discord", str(message.channel.id))
        if not dests:
            return

        logger.info(f"Discord -> Bridge: broadcasting message from {message.author}")
        
        avatar_url = str(message.author.display_avatar.url)
        
        for row in dests:
            dest_platform = row['platform']
            dest_channel_id = row['channel_id']
            
            if dest_platform == 'stoat':
                await self.send_to_stoat(dest_channel_id, message.content, message.author.display_name, avatar_url)
            # Future: forward to other discord channels too if needed

    async def send_to_stoat(self, channel_id, content, username, avatar_url):
        if not stoat_bot: return
        try:
            # We need to fetch the stoat channel object.
            # Unlike discord, stoat.py might need us to fetch it or construct it.
            # stoat.py Client.fetch_channel or similar.
            channel = await stoat_bot.fetch_channel(channel_id) # channel_id is string
            await channel.send(
                content=content[:2000],
                masquerade=stoat.Masquerade(
                    name=username[:32],
                    avatar=avatar_url,
                ),
            )
            logger.info(f"Discord -> Stoat (Ch: {channel_id}): sent.")
        except Exception as e:
             logger.error(f"Discord -> Stoat: Failed to send to {channel_id} - {e}")


# ----------------------------------------------------------------------
#  DISCORD COMMANDS
# ----------------------------------------------------------------------

# We use a Cog or a Group for "!dbridger bridge"
# But simply wrapping it in the main file for now.

@commands.group(name="dbridger")
async def dbridger(ctx):
    if ctx.invoked_subcommand is None:
        pass # await ctx.send("Invalid command. Use `!dbridger bridge ...`")

@dbridger.group(name="bridge")
async def bridge(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("Available subcommands: create, join, leave, status")

@bridge.command(name="create")
async def bridge_create(ctx):
    try:
        bridge_id = await create_bridge("discord", str(ctx.channel.id))
        # DM the user
        try:
            await ctx.author.send(f"Bridge created! ID: **{bridge_id}**. Keep it secret! Anyone with this ID can join your bridge.\nUse `!dbridger bridge join {bridge_id}` in other channels.")
            await ctx.send("Bridge created! I've sent you the ID in DMs.")
        except discord.Forbidden:
             await ctx.send(f"Bridge created! ID: **{bridge_id}**. (I couldn't DM you, so here it is. Delete this message if you want to keep it private!)")
    except ValueError as e:
        await ctx.send(f"Error: {e}")

@bridge.command(name="join")
async def bridge_join(ctx, bridge_id: int):
    try:
        await join_bridge(bridge_id, "discord", str(ctx.channel.id))
        await ctx.author.send(f"Success! Channel {ctx.channel.name} joined bridge {bridge_id}.")
        await ctx.send("Joined the bridge!")
    except ValueError as e:
        await ctx.send(f"Error: {e}")
    except discord.Forbidden:
        await ctx.send("Joined the bridge! (I couldn't DM you confirmation).")

@bridge.command(name="leave")
async def bridge_leave(ctx):
    try:
        await leave_bridge("discord", str(ctx.channel.id))
        await ctx.send("Left the bridge.")
    except ValueError as e:
        await ctx.send(f"Error: {e}")

@bridge.command(name="status")
async def bridge_status(ctx):
    bridge_id, channels = await get_bridge_status("discord", str(ctx.channel.id))
    if not bridge_id:
        await ctx.send("This channel is not in a bridge.")
    else:
        text = f"**Bridge ID: {bridge_id}**\nConnected Channels:\n"
        for r in channels:
            text += f"- {r['platform']}: {r['channel_id']}\n"
        await ctx.send(text)

# ----------------------------------------------------------------------
#  MAIN
# ----------------------------------------------------------------------

async def main():
    if not all([DISCORD_BOT_TOKEN, STOAT_BOT_TOKEN, DATABASE_URL]):
        raise RuntimeError("Missing configuration in .env (DISCORD_BOT_TOKEN, STOAT_BOT_TOKEN, DATABASE_URL)")

    await init_db()

    global discord_bot, stoat_bot
    discord_bot = DiscordBot()
    stoat_bot   = StoatBot(token=STOAT_BOT_TOKEN)

    # Register commands
    discord_bot.add_command(dbridger)

    logger.info("Starting bridges...")
    await asyncio.gather(
        discord_bot.start(DISCORD_BOT_TOKEN),
        stoat_bot.start(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bridge stopped")

