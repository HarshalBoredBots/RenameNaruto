"""
bot.py — Entry point.
Koyeb-friendly: starts a minimal aiohttp health-check server alongside the bot.
"""

import asyncio
import logging
import os
from datetime import datetime

import pyrogram.utils
from aiohttp import web
from pytz import timezone
from pyrogram import Client, __version__
from pyrogram.raw.all import layer

from config import Config
from helper.database import jishubotz
from messages import log, Msg
from route import web_server

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _noisy in ("pyrogram", "aiohttp", "motor"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Expand Pyrogram's ID range (required for large channels) ──────────────────
pyrogram.utils.MIN_CHAT_ID    = -999_999_999_999
pyrogram.utils.MIN_CHANNEL_ID = -1_009_999_999_999


class Bot(Client):

    def __init__(self):
        super().__init__(
            name="renamer",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=Config.BOT_TOKEN,
            # workers = Pyrogram's update-dispatcher thread pool.
            # Each concurrent rename job makes multiple API calls (edit, send,
            # copy). With workers=4 and 4 active jobs, every API call queues
            # behind the others. Raise to 8 so the dispatcher never becomes
            # the bottleneck on a Heroku 2X dyno (2 vCPU, 1 GB RAM).
            workers=8,
            plugins={"root": "plugins"},
            # sleep_threshold=0: do NOT let Pyrogram auto-sleep on FloodWait.
            # When Pyrogram sleeps it blocks the entire worker thread, stalling
            # ALL queued API calls. With threshold=0, FloodWait bubbles up as
            # an exception that our pipeline catches and handles per-task.
            sleep_threshold=0,
            # Set high enough that 4 concurrent jobs (each doing download +
            # upload) never queue at the MTProto transport layer.
            max_concurrent_transmissions=8,
        )

    async def start(self):
        await super().start()
        me            = await self.get_me()
        self.mention  = me.mention
        self.username = me.username
        self.uptime   = Config.BOT_UPTIME

        # ── MongoDB indexes (idempotent — safe to run every boot) ───────────
        await jishubotz.ensure_indexes()

        # ── Load persisted concurrency into the central rename queue ─────────
        from helper.queue_manager import rq as _rq_init
        await _rq_init.load_from_db()
        # Also call the shim for any legacy code that reads load_limits_from_db
        from plugins.file_rename import load_limits_from_db
        await load_limits_from_db()

        # ── Health-check web server (Koyeb / Render keep-alive) ───────────────
        port = int(os.environ.get("PORT", 8000))
        runner = web.AppRunner(await web_server())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info(f"Health-check server running on port {port}")

        # ── Auto-rename queue scheduler ───────────────────────────────────────
        from plugins.auto_rename import start_scheduler
        start_scheduler(self)

        # ── Register Telegram command menu ────────────────────────────────────
        # Derived from USER_CMDS / ADMIN_CMDS in help_menu.py + COMMAND_MAP.
        # Skips the API call if nothing changed since last boot.
        from helper.bot_commands import update_bot_commands
        await update_bot_commands(self)

        # ── Userbot for >2 GB files (optional) ───────────────────────────────
        from helper.userbot import get_userbot, userbot_available
        if userbot_available():
            ub = await get_userbot()
            if ub:
                log.info("Userbot ready — large-file (>2 GB) support enabled.")
            else:
                log.warning("STRING_SESSION set but userbot failed to start.")
        else:
            log.info("STRING_SESSION not set — large-file support disabled.")

        log.info(Msg.BOT_STARTED, name=me.first_name)

        # ── Notify admins ─────────────────────────────────────────────────────
        for admin_id in Config.ADMIN:
            try:
                await self.send_message(
                    admin_id,
                    f"╭━━━〔 ✨ TEMPEST ONLINE 〕━━━╮\n"
                    f"┃  ⚡  {me.mention} is online.\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
            except Exception as e:
                log.warning(Msg.BOT_ADMIN_NOTIFY_ERR, admin_id=admin_id, error=e)

        # ── Log to channel ────────────────────────────────────────────────────
        if Config.LOG_CHANNEL:
            try:
                ist = datetime.now(timezone("Asia/Kolkata"))
                await self.send_message(
                    Config.LOG_CHANNEL,
                    f"╭━━━〔 🌌 RIMURU SYSTEM BOOT 〕━━━╮\n"
                    f"┃  🤖  {me.mention}\n"
                    f"┃  📅  {ist.strftime('%d %B %Y')}\n"
                    f"┃  ⏰  {ist.strftime('%I:%M:%S %p')} IST\n"
                    f"┃  🔧  v{__version__} · Layer {layer}\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
            except Exception as e:
                log.warning(Msg.BOT_LOG_CHANNEL_ERR, error=e)

    async def stop(self):
        from helper.userbot import stop_userbot
        await stop_userbot()
        await super().stop()
        log.info(Msg.BOT_STOPPED, mention=self.mention)


Bot().run()
