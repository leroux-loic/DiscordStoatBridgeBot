#!/usr/bin/env python3
"""
Discord <-> Stoat bidirectional bridge.

Requirements:
    pip install discord.py stoat.py python-dotenv

Configuration:
    Copy .env.example to .env and fill in your tokens and IDs.

Usage:
    python bridge.py

NOTE: Do NOT rename this file to stoat.py – it conflicts with the library!
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv
import stoat

load_dotenv()

# ----------------------------------------------------------------------
#  CONFIGURATION  (set these in your .env file)
# ----------------------------------------------------------------------

DISCORD_BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

STOAT_BOT_TOKEN  = os.getenv("STOAT_BOT_TOKEN", "")
STOAT_CHANNEL_ID = os.getenv("STOAT_CHANNEL_ID", "")

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

discord_webhook: discord.Webhook | None = None
stoat_channel = None

# ----------------------------------------------------------------------
#  STOAT BOT
# ----------------------------------------------------------------------

class StoatBot(stoat.Client):

    async def on_ready(self, event, /):
        global stoat_channel
        logger.info(f"Stoat: connected as {self.me}")
        try:
            stoat_channel = await self.fetch_channel(STOAT_CHANNEL_ID)
            logger.info(f"Stoat: listening in #{stoat_channel.name} (ID: {STOAT_CHANNEL_ID})")
        except Exception as e:
            logger.error(f"Stoat: could not fetch channel {STOAT_CHANNEL_ID} – {e}")

    async def on_message_create(self, event: stoat.MessageCreateEvent, /):
        global discord_webhook
        msg = event.message

        if msg.author_id == self.me.id:
            return
    
        # Robust ID comparison (handle potential string vs int mismatch)
        if str(msg.channel.id) != str(STOAT_CHANNEL_ID):
            # logger.debug(f"Stoat -> Discord: dropped – wrong channel (msg: {msg.channel.id}, cfg: {STOAT_CHANNEL_ID})")
            return

        if not msg.content:
            logger.info("Stoat -> Discord: dropped – empty content")
            return

        author_name = msg.author.display_name or msg.author.name
        avatar      = msg.author.avatar
        avatar_url  = avatar.url() if avatar else None

        if discord_webhook is None:
            logger.error("Stoat -> Discord: dropped – webhook is None (bridge not ready)")
            return

        try:
            await discord_webhook.send(
                content=msg.content[:2000],
                username=author_name[:80],
                avatar_url=avatar_url,
                wait=True,
            )
            logger.info(f"Stoat -> Discord: sent message from {author_name}")
        except Exception as e:
            logger.error(f"Stoat -> Discord: {e}")

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

    async def setup_hook(self):
        self.loop.create_task(self._setup_webhook())

    async def _setup_webhook(self):
        global discord_webhook
        await self.wait_until_ready()

        try:
            channel = (
                self.get_channel(DISCORD_CHANNEL_ID)
                or await self.fetch_channel(DISCORD_CHANNEL_ID)
            )
        except Exception as e:
            logger.error(f"Discord: could not fetch channel {DISCORD_CHANNEL_ID} - {e}")
            return

        for wh in await channel.webhooks():
            if wh.user == self.user:
                discord_webhook = wh
                logger.info(f"Discord: reusing webhook '{wh.name}'")
                return

        discord_webhook = await channel.create_webhook(name="Stoat Bridge")
        logger.info("Discord: created new webhook")

    async def on_ready(self):
        logger.info(f"Discord: connected as {self.user}")

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id:
            return
        
        # Debug: Log all messages in the tracked channel
        if message.channel.id == DISCORD_CHANNEL_ID:
            logger.info(f"Discord -> Stoat: processing message from {message.author}...")
        else:
             # Ignore other channels silently
            return

        if not message.content:
            logger.info("Discord -> Stoat: dropped - no content")
            return

        if stoat_channel is None:
            logger.warning("Discord -> Stoat: dropped – stoat_channel is None (bridge not ready)")
            return

        avatar_url = (
            str(message.author.avatar.url)
            if message.author.avatar
            else str(message.author.default_avatar.url)
        )

        try:
            logger.info(f"Discord -> Stoat: sending '{message.content[:20]}...' to Stoat channel {STOAT_CHANNEL_ID}")
            await stoat_channel.send(
                content=message.content[:2000],
                masquerade=stoat.Masquerade(
                    name=message.author.display_name[:32],
                    avatar=avatar_url,
                ),
            )
            logger.info("Discord -> Stoat: sent message successfully")
        except Exception as e:
            logger.error(f"Discord -> Stoat: FAILED – {e}")

# ----------------------------------------------------------------------
#  MAIN
# ----------------------------------------------------------------------

async def main():
    if not all([DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, STOAT_BOT_TOKEN, STOAT_CHANNEL_ID]):
        raise RuntimeError("Missing configuration – check your .env file.")

    logger.info("Bridge starting...")
    await asyncio.gather(
        StoatBot(token=STOAT_BOT_TOKEN).start(),
        DiscordBot().start(DISCORD_BOT_TOKEN),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bridge stopped")
