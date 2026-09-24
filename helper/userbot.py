"""
helper/userbot.py
──────────────────
Lazily initialised Pyrogram userbot client used to handle files > 2 GB.

Client selection rule
─────────────────────
    file_size <= 2 GB  →  Normal Bot Client
    file_size >  2 GB  →  Premium String Session (userbot)

Usage
-----
    from helper.userbot import get_userbot, userbot_available, choose_client

    client_for_download = await choose_client(bot_client, file_size)
"""

from __future__ import annotations

import logging
from typing import Optional

from pyrogram import Client

from config import Config

logger = logging.getLogger(__name__)

_TWO_GB: int = 2 * 1024 * 1024 * 1024   # 2,147,483,648 bytes

_userbot: Optional[Client] = None
_started: bool = False


def userbot_available() -> bool:
    """True when a STRING_SESSION is configured."""
    return bool(getattr(Config, "STRING_SESSION", ""))


async def get_userbot() -> Optional[Client]:
    """
    Return the running userbot Client, starting it on first call.
    Returns None if STRING_SESSION is not set or startup failed.
    """
    global _userbot, _started

    if not getattr(Config, "STRING_SESSION", ""):
        return None

    if _started and _userbot is not None:
        return _userbot

    try:
        _userbot = Client(
            name="userbot",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=Config.STRING_SESSION,
            no_updates=True,      # only used for file transfer
            in_memory=True,
        )
        await _userbot.start()
        me = await _userbot.get_me()
        logger.info("Userbot started: %s (id=%s)", me.first_name, me.id)
        _started = True
        return _userbot
    except Exception as e:
        logger.error("Userbot failed to start: %s", e)
        _userbot = None
        _started = False
        return None


async def stop_userbot() -> None:
    """Gracefully stop the userbot (called from bot shutdown)."""
    global _userbot, _started
    if _userbot and _started:
        try:
            await _userbot.stop()
        except Exception:
            pass
        _userbot = None
        _started = False


async def choose_client(
    bot_client: Client,
    file_size: int,
    job_id: str = "?",
) -> tuple[Client, bool]:
    """
    Select the appropriate Pyrogram client based on file_size.

    Returns:
        (client, is_userbot)

    Rules:
        file_size <= 2 GB  →  (bot_client, False)
        file_size >  2 GB  →  (userbot,    True)   if userbot available

    Logs which client was chosen.
    Raises RuntimeError if >2 GB but userbot is unavailable.
    """
    size_mb = file_size / (1024 * 1024)
    size_gb = file_size / (1024 * 1024 * 1024)

    if file_size <= _TWO_GB:
        logger.info(
            "[client_select] job=%s  file_size=%.2f GB → using normal bot",
            job_id, size_gb,
        )
        return bot_client, False

    # > 2 GB — must use userbot
    logger.info(
        "[client_select] job=%s  file_size=%.2f GB → using premium String Session",
        job_id, size_gb,
    )

    if not userbot_available():
        raise RuntimeError("File > 2 GB but STRING_SESSION is not configured.")

    ub = await get_userbot()
    if ub is None:
        raise RuntimeError("File > 2 GB but userbot failed to start — check STRING_SESSION.")

    return ub, True
