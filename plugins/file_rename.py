"""
plugins/file_rename.py
Rename pipeline — fully concurrent, correct filenames, premium-gated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time

from pyrogram import Client, filters
from pyrogram.enums import MessageMediaType
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    Message,
)

from config import Config
from helper.database import jishubotz
from helper.ffmpeg import (
    add_metadata,
    fix_thumb,
    get_duration_hachoir,
    run_blocking,
    take_screen_shot,
)
from helper.utils import add_prefix_suffix, convert, humanbytes
from helper.queue_manager import qm as _qm, rq as _rq
from messages import log, Msg

logger = logging.getLogger(__name__)

_VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".ts",  ".m4v", ".wmv", ".3gp",
)
_CBZ_PDF_EXTS = (".cbz", ".pdf")


# ─────────────────────────────────────────────────────────────────────────────
# In-memory caches  (keyed by Telegram message_id)
# ─────────────────────────────────────────────────────────────────────────────
_pending:            dict[int, str]    = {}   # msg_id → exact user filename
from collections import OrderedDict

# Bounded LRU cache for original file messages (keyed by sent message ID).
# Max 500 entries — at 4 concurrent jobs this holds ~125 days of history,
# far more than needed. Oldest entries evicted automatically.
class _LRUCache(OrderedDict):
    def __init__(self, maxsize=500):
        super().__init__()
        self._maxsize = maxsize
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._maxsize:
            self.popitem(last=False)
    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value
    def get(self, key, default=None):
        try: return self[key]
        except KeyError: return default

_file_cache: _LRUCache = _LRUCache(maxsize=500)
_upload_type_cache:  dict[int, str]    = {}   # msg_id → callback_data string
_manual_tasks:       dict[str, object] = {}   # job_id → asyncio.Task (manual pipeline)
_mr_counter_val:     int               = 0    # sequential manual-rename job counter
_mr_counter_lock     = asyncio.Lock()


async def _new_mr_job_id() -> str:
    """Return next sequential manual-rename job ID like mr000001."""
    global _mr_counter_val
    async with _mr_counter_lock:
        _mr_counter_val += 1
        return f"mr{_mr_counter_val:06d}"


def _mr_counter() -> int:
    """Synchronous shortcut — only safe to call from async context as a stub."""
    return _mr_counter_val


# ══════════════════════════════════════════════════════════════════════════════
# Concurrency control  ── now owned entirely by helper.queue_manager.rq
# ══════════════════════════════════════════════════════════════════════════════

def get_transmission_sem() -> asyncio.Semaphore:
    """
    Backward-compat shim used by auto_rename.py's download/upload guards.
    Returns a semaphore whose value matches the current rq concurrency.
    The semaphore is rebuilt whenever /limit changes it.
    """
    return _get_or_build_tsem()


_tsem_cache: list = [None, 0]   # [semaphore, last_concurrency]

def _get_or_build_tsem() -> asyncio.Semaphore:
    """Return a Semaphore matching rq.concurrency, rebuilding if needed."""
    c = _rq.concurrency
    if _tsem_cache[0] is None or _tsem_cache[1] != c:
        _tsem_cache[0] = asyncio.Semaphore(c)
        _tsem_cache[1] = c
    return _tsem_cache[0]


async def load_limits_from_db() -> None:
    """
    Load persisted rename concurrency from MongoDB on bot startup.
    Delegates to rq.load_from_db() — one concurrency value, no complexity.
    """
    try:
        await _rq.load_from_db()
        logger.info("[limits] Loaded rename concurrency=%d from DB", _rq.concurrency)
    except Exception as e:
        logger.warning("[limits] Could not load from DB, using default: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# Manual-rename restart recovery
# ══════════════════════════════════════════════════════════════════════════════

_MANUAL_JOB_MAX_AGE: int = 86_400   # 24 hours — same policy as auto-rename

async def restore_manual_jobs(bot: "Client") -> None:
    """
    On startup: reload every manual-rename job that was in-flight when the bot
    restarted, and re-present it to the user so they can re-confirm or cancel.

    We cannot fully resume a manual rename automatically because the confirm
    callback (upload_type choice) is ephemeral — the callback message may have
    expired. Instead we:
      1. Notify the user which files were interrupted.
      2. Ask them to re-send the file if they still want to rename it.
      3. Clean up the DB record.

    This is intentionally conservative: replaying an upload_type callback
    against an old message_id is unreliable. The honest UX is to inform
    and let the user decide.
    """
    await asyncio.sleep(3)   # after auto-rename restore

    logger.info("[mr-recovery] Checking for interrupted manual-rename jobs...")

    try:
        pending = await jishubotz.load_all_pending_jobs()
    except Exception as e:
        logger.error("[mr-recovery] Failed to load pending jobs: %s", e)
        return

    now = time.time()
    manual_pending = []
    for doc in pending:
        if doc.get("job_type", "auto") != "manual":
            continue
        age = now - float(doc.get("queued_at", now))
        if age > _MANUAL_JOB_MAX_AGE:
            logger.info("[mr-recovery] Dropping stale manual job %s (age %.1f h)", doc["_id"], age / 3600)
            asyncio.create_task(jishubotz.delete_pending_job(doc["_id"]))
            continue
        manual_pending.append(doc)

    if not manual_pending:
        logger.info("[mr-recovery] No interrupted manual-rename jobs found")
        return

    logger.info("[mr-recovery] Found %d interrupted manual job(s)", len(manual_pending))

    notified: set[int] = set()

    for doc in manual_pending:
        job_id      = doc["_id"]
        user_id     = int(doc["user_id"])
        chat_id     = int(doc["chat_id"])
        file_name   = doc.get("file_name", "unknown")
        new_fname   = doc.get("new_filename", "")
        queued_at   = float(doc.get("queued_at", now))
        age_min     = (now - queued_at) / 60

        # Notify once per user even if they had multiple jobs
        header_needed = user_id not in notified
        notified.add(user_id)

        try:
            header = (
                "╭━━━〔 ⚠️ RENAME INTERRUPTED 〕━━━╮\n"
                "┃  The bot restarted mid-rename.\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            ) if header_needed else ""

            await bot.send_message(
                chat_id,
                f"{header}"
                f"╭━━━〔 🔁 MANUAL JOB LOST 〕━━━╮\n"
                f"┃  🆔  <code>{job_id}</code>\n"
                f"┃  📂  <code>{file_name[:40]}</code>\n"
                + (f"┃  ✏️   → <code>{new_fname[:40]}</code>\n" if new_fname else "")
                + f"┃  ⏱️  {age_min:.0f} min ago\n"
                f"┃\n"
                f"┃  Re-send the file to rename it again.\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
            )
        except Exception as _ne:
            logger.warning("[mr-recovery] Could not notify user %s: %s", user_id, _ne)

        # Clean up DB record — job cannot be auto-resumed
        try:
            await jishubotz.delete_pending_job(job_id)
        except Exception:
            pass

        await asyncio.sleep(0.2)

    logger.info("[mr-recovery] Manual-rename recovery complete — notified for %d job(s)", len(manual_pending))


# ══════════════════════════════════════════════════════════════════════════════
# Admin: /limit  /jobs
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("limit") & filters.user(Config.ADMIN))
async def cmd_limit(client: Client, message: Message):
    """
    /limit          — show current concurrency and queue state
    /limit <n>      — set concurrency to n (live, persisted)
    """
    parts = message.text.strip().split()

    if len(parts) == 1:
        # Status display
        active  = _rq.active_count()
        queued  = _rq.queued_count()
        c       = _rq.concurrency
        per_user: dict[int, int] = {}
        for j in _rq.active_jobs():
            per_user[j.user_id] = per_user.get(j.user_id, 0) + 1
        per_user_lines = "\n".join(
            f"┃  └ 🆔 <code>{uid}</code>  ·  {cnt} active"
            for uid, cnt in per_user.items()
        ) or "┃  └ None"
        return await message.reply_text(
            f"╭━━━〔 ⚙️ RENAME QUEUE 〕━━━╮\n"
            f"┃  🔢  Concurrency    ·  <code>{c}</code>\n"
            f"┃  ⚡  Active jobs    ·  <code>{active}</code>\n"
            f"┃  ⏳  Queued jobs    ·  <code>{queued}</code>\n"
            f"┃  💚  Free slots     ·  <code>{_rq.available_slots()}</code>\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"<b>Active per user:</b>\n{per_user_lines}\n\n"
            f"<i>Use /limit &lt;n&gt; to change concurrency.</i>"
        )

    try:
        n = int(parts[1])
        if n < 1:
            raise ValueError
    except (ValueError, IndexError):
        return await message.reply_text("❌ Usage: <code>/limit &lt;n&gt;</code> where n ≥ 1.")

    old = _rq.concurrency
    await _rq.set_concurrency(n)
    await jishubotz.set_rename_concurrency(n)
    # Invalidate the tsem cache so auto_rename picks up the new value
    _tsem_cache[0] = None

    active = _rq.active_count()
    queued = _rq.queued_count()
    note   = ""
    if n > old:
        note = f"\n┃  ▶️  Starting up to <code>{n - old}</code> queued job(s)."
    elif n < old:
        note = f"\n┃  ℹ️  Existing active jobs finish naturally; new limit applies from now."

    await message.reply_text(
        f"╭━━━〔 ✅ CONCURRENCY UPDATED 〕━━━╮\n"
        f"┃  🔢  New concurrency  ·  <code>{n}</code>  (was {old})\n"
        f"┃  ⚡  Active jobs      ·  <code>{active}</code>\n"
        f"┃  ⏳  Queued jobs      ·  <code>{queued}</code>\n"
        f"┃  💾  Persisted        ·  ✅{note}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


# Keep /setlimit and /getlimit as aliases / backward-compat for admins
@Client.on_message(filters.command("setlimit") & filters.user(Config.ADMIN))
async def cmd_setlimit(client: Client, message: Message):
    """
    Backward-compat: /setlimit auto <n> and /setlimit manual <n> still control
    per-day usage limits.  /setlimit <n> (just a number) sets concurrency.
    """
    parts = message.text.strip().split()
    if len(parts) == 2:
        # /setlimit 4  → treated as /limit 4
        try:
            n = int(parts[1])
            if n < 1:
                raise ValueError
        except ValueError:
            return await message.reply_text("❌ Usage: <code>/setlimit &lt;n&gt;</code>")
        old = _rq.concurrency
        await _rq.set_concurrency(n)
        await jishubotz.set_rename_concurrency(n)
        _tsem_cache[0] = None
        return await message.reply_text(
            f"✅ Concurrency set to <code>{n}</code> (was {old}).\n"
            f"Active: <code>{_rq.active_count()}</code>  Queued: <code>{_rq.queued_count()}</code>"
        )
    if len(parts) == 3:
        scope = parts[1].lower()
        try:
            n = int(parts[2])
            if n < 1:
                raise ValueError
        except ValueError:
            return await message.reply_text("❌ Limit must be a positive integer.")
        if scope == "auto":
            await jishubotz.set_limits(auto_daily_limit=n)
            return await message.reply_text(
                f"╭━━━〔 ⚙️ AUTO DAILY LIMIT 〕━━━╮\n"
                f"┃  📊  New  ·  <code>{n}</code> files/day (free users)\n"
                f"┃  💾  Persisted  ·  ✅\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            )
        if scope == "manual":
            await jishubotz.set_limits(manual_daily_limit=n)
            return await message.reply_text(
                f"╭━━━〔 ⚙️ MANUAL DAILY LIMIT 〕━━━╮\n"
                f"┃  📊  New  ·  <code>{n}</code> files/day (free users)\n"
                f"┃  💾  Persisted  ·  ✅\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            )
        if scope in ("global", "user", "transmission"):
            # Redirect to concurrency
            old = _rq.concurrency
            await _rq.set_concurrency(n)
            await jishubotz.set_rename_concurrency(n)
            _tsem_cache[0] = None
            return await message.reply_text(
                f"✅ Concurrency set to <code>{n}</code> (was {old}).\n"
                f"Active: <code>{_rq.active_count()}</code>  Queued: <code>{_rq.queued_count()}</code>"
            )
    await message.reply_text(
        "╭━━━〔 ⚙️ SETLIMIT USAGE 〕━━━╮\n"
        "┃  /setlimit <n>        ·  set concurrency\n"
        "┃  /setlimit auto <n>   ·  auto-rename/day (free)\n"
        "┃  /setlimit manual <n> ·  manual rename/day (free)\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<i>Use /limit <n> to set concurrency directly.</i>"
    )


@Client.on_message(filters.command("getlimit") & filters.user(Config.ADMIN))
async def cmd_getlimit(client: Client, message: Message):
    db_auto   = (await jishubotz.get_limits())[2]
    db_manual = (await jishubotz.get_limits())[4]
    c         = _rq.concurrency
    active    = _rq.active_count()
    queued    = _rq.queued_count()
    per_user: dict[int, int] = {}
    for j in _rq.active_jobs():
        per_user[j.user_id] = per_user.get(j.user_id, 0) + 1
    per_user_lines = "\n".join(
        f"┃  └ 🆔 <code>{uid}</code>  ·  {cnt} active"
        for uid, cnt in per_user.items()
    ) or "┃  └ None"
    await message.reply_text(
        f"╭━━━〔 ⚙️ LIMITS 〕━━━╮\n"
        f"┃  🔢  Concurrency        ·  <code>{c}</code>\n"
        f"┃  ⚡  Active rename jobs ·  <code>{active}</code>\n"
        f"┃  ⏳  Queued rename jobs ·  <code>{queued}</code>\n"
        f"┃  💚  Free slots         ·  <code>{_rq.available_slots()}</code>\n"
        f"┃  ✏️  Manual rename/day  ·  <code>{db_manual}</code> (free users)\n"
        f"┃  🔄  Auto-rename/day    ·  <code>{db_auto}</code> (free users)\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"<b>Active per user:</b>\n{per_user_lines}"
    )


@Client.on_message(filters.command("jobs") & filters.user(Config.ADMIN))
async def cmd_jobs(client: Client, message: Message):
    """Admin-facing /jobs — detailed view of all active + queued jobs."""
    await message.reply_text(
        _build_jobs_text(),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="admin_jobs_refresh"),
        ]]),
    )


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _build_jobs_text() -> str:
    active_jobs = _rq.active_jobs()
    queued_jobs = _rq.queued_jobs()
    c           = _rq.concurrency
    t_active    = _rq.active_count()
    t_queued    = _rq.queued_count()

    def _trim_name(n, maxlen=40):
        return n if len(n) <= maxlen else n[:maxlen - 1] + "…"

    # ── Header ────────────────────────────────────────────────────────────
    lines = [
        "╭━━━〔 📋 RENAME QUEUE 〕━━━╮",
        f"┃  ⚡  Active      ·  {t_active}/{c}",
        f"┃  ⏳  Queued      ·  {t_queued}",
        f"┃  💚  Free slots  ·  {_rq.available_slots()}",
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
        "",
    ]

    # ── Active jobs grouped by user ───────────────────────────────────────
    if active_jobs:
        lines.append("⚙️  <b>ACTIVE</b>")
        # group by user
        by_user: dict[int, list] = {}
        for j in active_jobs:
            by_user.setdefault(j.user_id, []).append(j)
        for uid, jobs in by_user.items():
            uname = jobs[0].user_name
            lines.append(f"  👤 <b>{uname}</b>  <code>[{uid}]</code>  ·  {len(jobs)} active")
            for j in jobs:
                name    = _trim_name(j.filename)
                running = _fmt_dur(j.run_seconds())
                lines.append(f"     ▶ <code>{j.job_id}</code>  ·  {running}  ·  <i>{name}</i>")
        lines.append("")

    # ── Queued jobs with fair position ────────────────────────────────────
    if queued_jobs:
        lines.append("⏳  <b>QUEUE</b>")
        # Group queued by user for the display, but show global fair positions
        for pos, j in enumerate(queued_jobs, start=1):
            fair_pos = _rq.queue_position(j.job_id)
            name     = _trim_name(j.filename)
            waited   = _fmt_dur(j.wait_seconds())
            uname    = j.user_name
            pos_str  = f"#{fair_pos}" if fair_pos > 0 else "next"
            lines.append(
                f"  {pos_str}  <code>{j.job_id}</code>  ·  {uname}  ·  wait {waited}\n"
                f"       <i>{name}</i>"
            )
        lines.append("")

    if t_active == 0 and t_queued == 0:
        lines.append("✨  Queue is empty.")

    return "\n".join(lines)


@Client.on_callback_query(filters.regex(r"^admin_jobs_refresh$"))
async def cb_admin_jobs_refresh(client: Client, update):
    if update.from_user.id not in Config.ADMIN:
        return await update.answer("⛔ Admin only.", show_alert=True)
    try:
        await update.message.edit_text(
            _build_jobs_text(),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="admin_jobs_refresh"),
            ]]),
        )
        await update.answer("✅ Refreshed")
    except Exception:
        await update.answer("No changes.")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — File received → premium gate → show action buttons
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & (filters.document | filters.audio | filters.video))
async def rename_start(client: Client, message: Message):
    file    = getattr(message, message.media.value)
    user_id = int(message.from_user.id)

    # ── Single round-trip: ban check + mode + premium ─────────────────────
    if await jishubotz.is_banned(user_id):
        return await message.reply(
            "╭━━━〔 🛡 ACCESS DENIED 〕━━━╮\n"
            "┃  ⛔  Your account is restricted.\n"
            "┃  Contact @naruto0927 to appeal.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    # ── Auto-rename mode: NO premium gate — free users can use auto-rename ─
    rename_mode = await jishubotz.get_rename_mode(user_id)
    if rename_mode == "auto":
        from plugins.auto_rename import run_auto_rename
        return await run_auto_rename(client, message)

    # ── Manual rename: free users have a daily cap; premium = unlimited ─────
    # Parallel DB reads — resolves premium status and daily count in one gather
    _ps_check, (_auto_lim, _manual_lim) = await asyncio.gather(
        jishubotz.get_pipeline_settings(user_id),
        jishubotz.get_daily_limits_cached(),
    )
    _is_prem = _ps_check["premium"]
    if not _is_prem:
        _used_today = _ps_check["manual_daily_count"]
        _day_limit  = _manual_lim
        if _used_today >= _day_limit:
            return await message.reply_text(
                f"╭━━━〔 ⚡ DAILY LIMIT REACHED 〕━━━╮\n"
                f"┃  📊  Used  ·  {_used_today} / {_day_limit} today\n"
                f"┃  ⏱   Resets at midnight UTC\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"Upgrade to <b>Tempest Elite</b> for unlimited renames 👑",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👑 My Status", callback_data="check_premium_status"),
                ]])
            )

    # ── File size gate ────────────────────────────────────────────────────
    from helper.userbot import userbot_available
    is_large = file.file_size > Config.BOT_MAX_SIZE
    if is_large:
        # Premium already confirmed above — just check userbot availability
        if not userbot_available():
            return await message.reply_text(
                "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                "┃  📦  File exceeds 2 GB.\n"
                "┃  ⚡  STRING_SESSION not configured.\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "Contact the admin to enable large-file support."
            )
        if file.file_size > Config.USER_MAX_SIZE:
            return await message.reply_text(
                "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                "┃  📦  Exceeds 4 GB — maximum barrier.\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            )

    filename = file.file_name or ""
    ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    _audio_exts = (".mp3", ".flac", ".aac", ".ogg", ".opus", ".wav", ".m4a", ".wma", ".aiff")
    is_cbz_pdf  = ext in ("cbz", "pdf")
    is_audio    = (message.media == MessageMediaType.AUDIO) or ext in _audio_exts
    is_video    = (message.media == MessageMediaType.VIDEO) or ext in [e.lstrip(".") for e in _VIDEO_EXTS]

    row1 = [InlineKeyboardButton("📄 Document", callback_data="upload_document")]
    if is_video:
        row1.append(InlineKeyboardButton("🎬 Video", callback_data="upload_video"))
    if is_audio:
        row1.append(InlineKeyboardButton("🎵 Audio", callback_data="upload_audio"))

    row2 = []
    if is_cbz_pdf:
        row2.append(InlineKeyboardButton("📚 CBZ/PDF", callback_data="upload_cbzpdf"))
    row2.append(InlineKeyboardButton("📊 MediaInfo", callback_data="action_mediainfo"))

    buttons = [row1, row2]
    if is_video:
        buttons.append([
            InlineKeyboardButton("📸 Grid",   callback_data="media_screenshot"),
            InlineKeyboardButton("🎞️ Sample", callback_data="media_sample"),
        ])

    # Steal Thumb available for all file types
    buttons.append([
        InlineKeyboardButton("🪄 Steal Thumb", callback_data="action_steal_thumb"),
    ])



    action_text = (
        f"╭━━━〔 📂 FILE ACQUIRED 〕━━━╮\n"
        f"┃  📛  Name  ·  <code>{(filename or 'Unknown')[:40]}</code>\n"
        f"┃  📦  Size  ·  {humanbytes(file.file_size)}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"<i>⚡ Great Sage awaits your command.\n"
        f"Select an action below to begin.</i>"
    )
    pic = await jishubotz.get_pic("rename_pic")
    if pic:
        try:
            sent = await message.reply_photo(
                photo=pic,
                caption=action_text,
                reply_to_message_id=message.id,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            _file_cache[sent.id] = message
            return
        except Exception:
            pass

    sent = await message.reply(
        text=action_text,
        reply_to_message_id=message.id,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    _file_cache[sent.id] = message


@Client.on_callback_query(filters.regex("^check_premium_status$"))
async def cb_check_premium(bot, update):
    is_prem = await jishubotz.is_premium(update.from_user.id)
    if is_prem:
        await update.answer("✦ Premium active ✓", show_alert=True)
    else:
        await update.answer("✦ No premium. Contact admin.", show_alert=True)



# ══════════════════════════════════════════════════════════════════════════════
# MediaInfo button
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex("^action_mediainfo$"))
async def cb_mediainfo(bot, update):
    await update.answer("📊 Generating MediaInfo...")
    asyncio.create_task(_handle_mediainfo(bot, update))


async def _handle_mediainfo(bot, update) -> None:
    from plugins.mediainfo import (
        _ffprobe_sync,
        _build_telegraph_nodes,
        _build_plain_fallback,
        _partial_download      as _mi_partial_dl,
        _upload_to_telegraph   as _telegraph_upload,
    )

    chat_id = update.message.chat.id

    # Prefer _file_cache (set when the action keyboard was sent) over
    # reply_to_message — this works even when reply chains are broken
    # (e.g. photo replies) and avoids expired file_reference issues.
    file_message = (
        _file_cache.get(update.message.id)
        or update.message.reply_to_message
    )

    if not file_message or not file_message.media:
        return await update.message.edit("❌ <b>File not found.</b>")

    media     = getattr(file_message, file_message.media.value, None)
    raw_name  = getattr(media, "file_name", None) or f"file_{int(time.time())}"
    file_size = getattr(media, "file_size", 0)

    ms = await update.message.edit("⏳ <b>Status:</b> <code>[▒▒▒▒▒▒▒]</code> Fetching Source...")

    user_id  = update.from_user.id
    job_id   = f"mi_{user_id}_{int(time.time() * 1000)}"
    dl_dir   = f"downloads/{job_id}"
    os.makedirs(dl_dir, exist_ok=True)

    safe_name = "".join(c for c in raw_name if c.isalnum() or c in "._- []@")
    file_path = os.path.join(dl_dir, safe_name)

    try:
        partial_limit = min(int(file_size * 0.15), 50 * 1024 * 1024)
        partial_limit = max(partial_limit, 2 * 1024 * 1024)
        await _safe_edit(ms, f"⏳ <b>Fetching header</b>  {humanbytes(partial_limit)} / {humanbytes(file_size)}")

        file_path = await _mi_partial_dl(bot, file_message, file_path, partial_limit)

        if not file_path or not os.path.exists(file_path):
            return await _safe_edit(ms, "❌ <b>Internal Error</b>\nDownload failed. Please try again.")

        await _safe_edit(ms, "⚙️ <b>Status:</b> <code>[●●●●○○○]</code> Analysing Streams...")
        data = await run_blocking(_ffprobe_sync, file_path)

        await _safe_edit(ms, "📤 <b>Status:</b> <code>[███████]</code> Publishing Report...")
        bot_username = getattr(bot, "username", None) or "RimuruBot"
        nodes    = _build_telegraph_nodes(data, raw_name, file_size, bot_username)
        page_url = await _telegraph_upload(f"MediaInfo of {raw_name}", nodes, bot_username)

        if page_url:
            await ms.edit(
                f"📊 **MediaInfo**\n\n"
                f"📂 **File:** `{raw_name}`\n"
                f"📦 **Size:** `{humanbytes(file_size)}`\n\n"
                f"[📋 View Full MediaInfo]({page_url})",
                disable_web_page_preview=True,
            )
        else:
            # Telegraph failed — send plain text fallback inline
            plain   = _build_plain_fallback(data, raw_name, file_size)
            snippet = plain[:3800] + ("\n\n… (truncated)" if len(plain) > 3800 else "")
            await ms.edit(f"✦ <b>MediaInfo</b>\n\n<code>{snippet}</code>")

    except Exception as e:
        logger.error("MediaInfo error: %s", e)
        await _safe_edit(ms, f"❌ <b>Internal Error</b>\nMediaInfo failed: <code>{e}</code>")
    finally:
        _cleanup_dir(dl_dir)


# ══════════════════════════════════════════════════════════════════════════════
# Screenshot button
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex("^media_screenshot$"))
async def cb_screenshot(bot, update):
    await update.answer("📸 Generating screenshots...")
    asyncio.create_task(_handle_screenshot(bot, update))


async def _handle_screenshot(bot, update) -> None:
    from helper.ffmpeg import generate_screenshot_grid
    from plugins.mediainfo import _partial_download as _mi_partial_dl

    chat_id      = update.message.chat.id
    file_message = update.message.reply_to_message

    if not file_message or not file_message.media:
        return await update.message.edit("❌ <b>File not found.</b>")

    ms      = await update.message.edit("⏳ <b>Status:</b> <code>[▒▒▒▒▒▒▒]</code> Fetching Source...")
    user_id = update.from_user.id
    job_id  = f"ss_{user_id}_{int(time.time() * 1000)}"
    dl_dir  = f"downloads/{job_id}"
    os.makedirs(dl_dir, exist_ok=True)

    file     = getattr(file_message, file_message.media.value)
    filename = file.file_name or "video.mkv"
    dl_path  = f"{dl_dir}/{filename}"

    try:
        # Full download required — ffmpeg needs to seek to 20/40/60/80/95%
        # of the video duration for an evenly-spaced grid. Partial downloads
        # only contain early timestamps (everything comes out 00:00:xx).
        await _safe_edit(ms, "╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n┃  ⬇️  Acquiring file data...\n┃  <code>[░░░░░░░░░░]</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        try:
            await bot.download_media(message=file_message, file_name=dl_path)
        except Exception as e:
            return await _safe_edit(ms, f"╭━━━〔 ❌ SKILL FAILED 〕━━━╮\n┃  ⬇️  Download error:\n┃  <code>{e}</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        if not os.path.exists(dl_path) or os.path.getsize(dl_path) == 0:
            return await _safe_edit(ms, "❌ <b>Download failed.</b>")

        await _safe_edit(ms, "⚙️ <b>Status:</b> <code>[●●●●●○○]</code> Generating Grid...")

        ss_count  = await jishubotz.get_screenshot_count(update.from_user.id)
        # cols: 1 → 1, 2-3 → 2, 4+ → 3, 8+ → 4
        if ss_count <= 1:
            cols = 1
        elif ss_count <= 3:
            cols = 2
        elif ss_count <= 9:
            cols = 3
        else:
            cols = 4
        grid_path = await generate_screenshot_grid(dl_path, dl_dir, count=ss_count, cols=cols)

        if not grid_path:
            return await _safe_edit(ms, "❌ <b>Error:</b> Invalid video stream or codec.")

        await ms.delete()
        await bot.send_photo(
            chat_id,
            photo=grid_path,
            caption=f"◈ <b>Video Preview Grid</b>\n<blockquote><code>{filename}</code></blockquote>\n\n📸 <b>Layout:</b> {ss_count} Frames generated.",
        )

    except Exception as e:
        logger.error("Screenshot error: %s", e)
        await _safe_edit(ms, f"❌ <b>Internal Error</b>\nScreenshot failed: <code>{e}</code>")
    finally:
        _cleanup_dir(dl_dir)


# ══════════════════════════════════════════════════════════════════════════════
# Sample Video button
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex("^media_sample$"))
async def cb_sample_video(bot, update):
    await update.answer("🎬 Generating sample clip...")
    asyncio.create_task(_handle_sample_video(bot, update))


async def _handle_sample_video(bot, update) -> None:
    from helper.ffmpeg import generate_sample_video, get_video_duration
    from plugins.mediainfo import _partial_download as _mi_partial_dl

    chat_id      = update.message.chat.id
    file_message = update.message.reply_to_message

    if not file_message or not file_message.media:
        return await update.message.edit("❌ <b>File not found.</b>")

    ms      = await update.message.edit("⏳ <b>Fetching file header…</b>")
    user_id = update.from_user.id
    job_id  = f"smp_{user_id}_{int(time.time() * 1000)}"
    dl_dir  = f"downloads/{job_id}"
    os.makedirs(dl_dir, exist_ok=True)

    file        = getattr(file_message, file_message.media.value)
    filename    = file.file_name or "video.mkv"
    file_size   = getattr(file, "file_size", 0) or 0
    dl_path     = f"{dl_dir}/{filename}"
    sample_path = None

    # Strategy: download a partial chunk around the target sample window.
    # Step 1 — fetch first 5 MB to read container duration from the header.
    # Step 2 — compute the start offset (10–70% of duration).
    # Step 3 — for the sample window (30s at typical 2–4 Mbps ≈ 8–15 MB),
    #           download only that byte range via stream_media offset+limit.
    #           This means we download ~20 MB instead of 1–2 GB.
    _HEADER_BYTES = 5 * 1024 * 1024
    _SAMPLE_BYTES = 50 * 1024 * 1024   # 50 MB window — covers 30s at high bitrate

    try:
        # ── Phase 1: fetch header to read duration ────────────────────────────
        result = await _mi_partial_dl(bot, file_message, dl_path, _HEADER_BYTES)
        if not result:
            return await _safe_edit(ms, "❌ <b>Download failed.</b>")

        total_duration = await get_video_duration(dl_path)

        # ── Phase 2: compute sample start byte offset ─────────────────────────
        if total_duration > 0 and file_size > 0 and total_duration > 30:
            import random as _random
            lo    = total_duration * 0.10
            hi    = max(total_duration * 0.70, lo + 1.0)
            start_sec = _random.uniform(lo, hi)
            start_sec = min(start_sec, total_duration - 30.5)
            # Byte offset proportional to start time
            byte_offset = int((start_sec / total_duration) * file_size)
            byte_offset = max(0, byte_offset - 2 * 1024 * 1024)  # 2 MB before target
        else:
            byte_offset = 0

        # ── Phase 3: fetch just the sample window into a SEPARATE file ──────
        # IMPORTANT: do NOT append to dl_path. The header (Phase 1) already
        # lives there. Appending the window produces a non-contiguous byte
        # stream; ffmpeg cannot seek it and sample generation fails.
        # Instead write the window to its own temp file.
        await _safe_edit(ms, "⏳ <b>Status:</b> <code>[▒▒▒▒░░░]</code> Fetching Window...")
        window_path = f"{dl_dir}/window_{int(time.time())}.tmp"
        window_ok   = False
        try:
            written = 0
            with open(window_path, "wb") as wf:
                async for chunk in bot.stream_media(
                    file_message,
                    offset=byte_offset // (1024 * 1024),  # offset in MB (Pyrogram unit)
                    limit=_SAMPLE_BYTES,
                ):
                    wf.write(chunk)
                    written += len(chunk)
                    if written >= _SAMPLE_BYTES:
                        break
            if written > 0:
                window_ok = True
        except Exception:
            pass  # fall back to header-only path below

        # Always use the full file for sample generation.
        # Window files (partial downloads) may not have valid frame headers at the start,
        # causing ffmpeg to fail. Full file is safer and ffmpeg can seek within it.
        source_for_sample = dl_path

        await _safe_edit(ms, "⚙️ <b>Status:</b> <code>[●●●●●○○]</code> Trimming Clip...")

        sample_dur  = await jishubotz.get_sample_duration(update.from_user.id)
        sample_path = await generate_sample_video(source_for_sample, dl_dir, duration=sample_dur)

        if not sample_path:
            return await _safe_edit(ms, "❌ <b>Error:</b> Could not trim file. Check format.")

        await ms.delete()
        await bot.send_video(
            chat_id,
            video=sample_path,
            caption=f"◈ <b>Sample Generated</b>\n<blockquote><code>{filename}</code></blockquote>\n\n🎞️ <b>Duration:</b> {sample_dur} seconds.",
            supports_streaming=True,
        )

    except Exception as e:
        logger.error("Sample video error: %s", e)
        await _safe_edit(ms, f"❌ <b>Internal Error</b>\nSample failed: <code>{e}</code>")
    finally:
        if sample_path:
            _safe_remove(sample_path)
        # Clean up window temp file if it was created
        try:
            if "window_path" in dir() and window_path and os.path.exists(window_path):
                os.remove(window_path)
        except Exception:
            pass
        _cleanup_dir(dl_dir)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Rename button tapped → ask for filename
# ══════════════════════════════════════════════════════════════════════════════


@Client.on_callback_query(filters.regex(r"^manual_cancel_(.+)$"))
async def cb_manual_cancel(bot, update):
    """Cancel a running manual rename pipeline."""
    job_id = update.data[len("manual_cancel_"):]
    task   = _manual_tasks.get(job_id)

    if task and not task.done():
        task.cancel()
        await update.answer("🗑  Task cancelled.", show_alert=True)
        try:
            await update.message.edit(
                f"╭━━━〔 🗑 CANCELLED 〕━━━╮\n"
                f"┃  ⚡  Rename task removed.\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            )
        except Exception:
            pass
    else:
        await update.answer("⚠️  Task already finished or not found.", show_alert=True)

    _manual_tasks.pop(job_id, None)


@Client.on_callback_query(filters.regex("^upload_"))
async def ask_filename(bot, update):
    await update.answer()
    file_message = update.message.reply_to_message
    if not file_message or not file_message.media:
        return await update.message.edit("❌ <b>Internal Error</b>\nFile not found or expired.")

    file     = getattr(file_message, file_message.media.value)
    filename = file.file_name or "file"

    await update.message.delete()

    sent = await bot.send_message(
        update.message.chat.id,
        text=(
            f"◈ <b>Rename File</b>\n"
            f"<blockquote><b>Old:</b> <code>{filename}</code></blockquote>\n\n"
            f"➜ <i>Please send the <b>New Filename</b> now.</i>\n"
            f"➜ Use /cancel to abort."
        ),
        reply_markup=ForceReply(True),
    )

    _upload_type_cache[sent.id] = update.data
    _file_cache[sent.id]        = file_message


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — User typed filename → show confirm button
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.reply)
async def refunc(client: Client, message: Message):
    reply_message = message.reply_to_message
    if not (reply_message.reply_markup and isinstance(reply_message.reply_markup, ForceReply)):
        return

    upload_type_stored = _upload_type_cache.get(reply_message.id)
    file_message       = _file_cache.get(reply_message.id)

    if not upload_type_stored or not file_message:
        return

    new_name = message.text.strip()

    media = getattr(file_message, file_message.media.value)

    if "." not in new_name:
        extn = (
            media.file_name.rsplit(".", 1)[-1]
            if "." in (media.file_name or "")
            else "mkv"
        )
        new_name = f"{new_name}.{extn}"

    # Send confirm message BEFORE deleting anything.
    # message.reply() fails if message is already deleted.
    sent = await client.send_message(
        chat_id=message.chat.id,
        text=f"◈ <b>Final Review</b>\n<blockquote><b>New:</b> <code>{new_name}</code></blockquote>\n\n➜ <i>Proceed with this name?</i>",
        reply_to_message_id=file_message.id,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{upload_type_stored}")
        ]]),
    )

    _pending[sent.id] = new_name

    # Now delete the ForceReply prompt and user's name message
    _upload_type_cache.pop(reply_message.id, None)
    _file_cache.pop(reply_message.id, None)
    try: await reply_message.delete()
    except Exception: pass
    try: await message.delete()
    except Exception: pass


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Confirm → acquire slot → fire task (non-blocking)
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex("^confirm_upload_"))
async def doc(bot, update):
    user_id = update.from_user.id

    if not await jishubotz.is_premium(user_id):
        return await update.answer(
            "⛔ <b>Access Denied</b>\nYour premium has expired. Contact the admin to renew.",
            show_alert=True,
        )

    # ── Read the pending filename NOW (at callback time, not at execution time) ──
    # _pending is an in-memory dict keyed by the confirmation message ID.
    # If we let _pipeline read it later (after waiting in queue), a Heroku
    # restart or Telegram callback retry will have wiped/consumed the entry
    # already, causing "Metadata unreadable". Capture it here and pass it
    # directly into the job closure.
    _new_filename_raw = _pending.pop(update.message.id, None)
    if not _new_filename_raw:
        return await update.answer(
            "⚠️ Session expired — please send the file and rename it again.",
            show_alert=True,
        )

    # Snapshot thumbnail NOW at confirm time
    try:
        _locked_thumb = await jishubotz.get_thumbnail(user_id)
    except Exception:
        _locked_thumb = None

    _pre_job_id = await _new_mr_job_id()

    try:
        _file_msg  = update.message.reply_to_message
        _media_obj = getattr(_file_msg, _file_msg.media.value, None) if _file_msg and _file_msg.media else None
        _disp_name = getattr(_media_obj, "file_name", None) or "file"
    except Exception:
        _disp_name = "file"

    _uname = (getattr(update.from_user, "first_name", None) or
              getattr(update.from_user, "username", None) or
              str(user_id))

    active_count = _rq.active_count()
    concurrency  = _rq.concurrency

    # Pass _new_filename_raw directly into the closure — no _pending lookup later
    async def _job_fn():
        await _run_rename(bot, update, locked_thumb_id=_locked_thumb,
                          new_filename_raw=_new_filename_raw)

    rq_job = await _rq.enqueue(
        fn=_job_fn,
        job_id=_pre_job_id,
        user_id=user_id,
        meta={"filename": _disp_name, "type": "manual", "user_name": _uname},
    )

    # Feedback after enqueue so position is real
    my_active = _rq.user_active_count(user_id)
    if active_count < concurrency:
        pos_info = "▶️ Starting immediately"
    else:
        raw_pos = _rq.queue_position(_pre_job_id)
        pos_info = f"⏳ Queue position: {raw_pos}" if raw_pos > 0 else "▶️ Starting immediately"

    await update.answer(
        f"📥 {pos_info}  ·  {active_count}/{concurrency} active",
        show_alert=False,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Rename job wrapper — owns semaphore + slot lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def _run_rename(bot, update, locked_thumb_id: str | None = None,
                      new_filename_raw: str | None = None) -> None:
    """Runs inside a queue slot owned by rq."""
    user_id = update.from_user.id
    try:
        await _pipeline(bot, update, user_id, update.message.chat.id,
                        locked_thumb_id=locked_thumb_id,
                        new_filename_raw=new_filename_raw)
    except asyncio.CancelledError:
        logger.info("Task cancelled for user=%s", user_id)
        raise
    except Exception as e:
        logger.exception("Unhandled error in rename task user=%s: %s", user_id, e)


# ══════════════════════════════════════════════════════════════════════════════
# Core pipeline: download → (metadata) → upload
# ══════════════════════════════════════════════════════════════════════════════

async def _pipeline(bot, update, user_id: int, chat_id: int,
                    locked_thumb_id: str | None = None,
                    new_filename_raw: str | None = None) -> None:
    os.makedirs("Metadata", exist_ok=True)

    upload_type = update.data.split("_")[-1]

    # new_filename_raw is passed directly from the callback closure.
    # We no longer read _pending here — it was already popped at callback time
    # so a queue delay or restart cannot lose the filename.
    if not new_filename_raw:
        return await update.message.edit(
            "╭━━━〔 ❌ SYSTEM ERROR 〕━━━╮\n┃  Great Sage: Metadata unreadable.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    # ── ONE round-trip: load all pipeline settings ───────────────────────
    _ps = await jishubotz.get_pipeline_settings(chat_id)
    prefix = _ps["prefix"]
    suffix = _ps["suffix"]
    try:
        new_filename = add_prefix_suffix(new_filename_raw, prefix, suffix)
    except Exception as e:
        return await update.message.edit(
            f"❌ <b>Internal Error</b>\nPrefix/Suffix failed: <code>{e}</code>"
        )

    job_id       = await _new_mr_job_id()
    dl_dir       = f"downloads/{job_id}"
    os.makedirs(dl_dir, exist_ok=True)

    ext           = new_filename.rsplit(".", 1)[-1] if "." in new_filename else "mkv"
    file_path     = f"{dl_dir}/_tmp_{job_id}.{ext}"
    metadata_path = f"Metadata/{job_id}.{ext}"

    file           = update.message.reply_to_message
    ph_path        = None
    _bool_metadata = False

    # ── Persist to MongoDB so this job survives a restart ─────────────────
    _orig_fname_mr = getattr(
        getattr(file, file.media.value, None) if file.media else None,
        "file_name", None,
    ) or "unknown"
    _username_mr = getattr(getattr(update, "from_user", None), "username", None) or ""
    try:
        await jishubotz.save_pending_job(
            job_id        = job_id,
            user_id       = user_id,
            chat_id       = chat_id,
            message_id    = file.id,           # the MEDIA message, not the callback
            file_name     = _orig_fname_mr,
            queued_at     = time.time(),
            username      = _username_mr,
            job_type      = "manual",
            new_filename  = new_filename,
            upload_type   = upload_type,
        )
    except Exception as _pe:
        logger.warning("[pipeline] save_pending_job failed job=%s: %s", job_id, _pe)

    logger.info(
        "▶ PIPELINE START  user=%s  filename=%s  job=%s  active_total=%s",
        user_id, new_filename, job_id, _rq.active_count()
    )

    try:
        try:
            ms = await update.message.edit("╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n┃  ⬇️  Acquiring file data...\n┃  <code>[░░░░░░░░░░]</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        except Exception:
            ms = update.message

        # Cancel button — stays on the message throughout the job
        cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🗑 Cancel  [{job_id}]", callback_data=f"manual_cancel_{job_id}")
        ]])
        try:
            await ms.edit(
                "╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n"
                "┃  ⬇️  Acquiring file data...\n"
                "┃  <code>[░░░░░░░░░░]</code>\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
                reply_markup=cancel_kb,
            )
        except Exception:
            pass

        # Register this task so the cancel callback can reach it
        _manual_tasks[job_id] = asyncio.current_task()

        # ── Choose client: userbot for >2 GB, bot otherwise ───────────────────
        # file = update.message.reply_to_message (the original file message)
        from helper.userbot import get_userbot, userbot_available
        try:
            _media_obj = getattr(file, file.media.value) if file.media else None
            _file_size = getattr(_media_obj, "file_size", 0) or 0
        except Exception:
            _file_size = 0

        _large     = _file_size > Config.BOT_MAX_SIZE
        _dl_client = bot   # bot always downloads (reference pattern)
        _ul_client = bot   # bot for ≤2 GB; overridden to userbot for upload relay
        _ub_manual = None

        if _large and userbot_available():
            _ub_manual = await get_userbot()
            if _ub_manual:
                _ul_client = _ub_manual   # userbot only for large upload relay
                logger.info("[pipeline] job=%s large file %s MB — bot downloads, userbot relays",
                            job_id, _file_size // 1024 // 1024)
            else:
                logger.warning("[pipeline] job=%s userbot unavailable — bot limit applies", job_id)

        # ── Download — slot already owned by rq; just download ──────────────
        try:
            await bot.download_media(   # always bot — avoids PEER_ID_INVALID
                message=file,
                file_name=file_path,
                progress=_pipeline_progress,
                progress_args=(job_id, "Downloading", ms, time.time(), cancel_kb),
            )
        except asyncio.CancelledError:
            await _safe_edit(ms, "🛑 <b>Status:</b> <code>[✖]</code> Session Cancelled.")
            return
        except Exception as e:
            return await _safe_edit(ms, f"╭━━━〔 ❌ SKILL FAILED 〕━━━╮\n┃  ⬇️  Download error:\n┃  <code>{e}</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")

        duration = await get_duration_hachoir(file_path)

        # ── Thumbnail ─────────────────────────────────────────────────────────
        # locked_thumb_id was snapshotted at confirm time — immune to
        # mid-rename thumb changes by the user.
        media   = getattr(file, file.media.value)
        # locked_thumb_id (from confirm callback) takes priority;
        # fall back to the pre-loaded settings thumbnail
        c_thumb = locked_thumb_id or _ps["thumbnail"]

        if c_thumb:
            try:
                dl = await bot.download_media(c_thumb)
                if dl and os.path.exists(dl) and os.path.getsize(dl) > 0:
                    _, __, ph_path = await fix_thumb(dl)
                else:
                    if dl and os.path.exists(dl):
                        os.remove(dl)
                    ph_path = None
            except Exception as e:
                logger.warning("Custom thumbnail download failed (%s) — using auto thumb", e)
                await jishubotz.set_thumbnail(chat_id, file_id=None)
                ph_path = None

        if ph_path is None and media.thumbs:
            try:
                ph_path_ = await take_screen_shot(
                    file_path, dl_dir,
                    random.randint(0, max(duration - 1, 0)),
                )
                if ph_path_ and os.path.exists(ph_path_) and os.path.getsize(ph_path_) > 0:
                    _, __, ph_path = await fix_thumb(ph_path_)
            except Exception as e:
                ph_path = None
                logger.warning("Auto thumbnail error: %s", e)

        # ── Caption (from pre-loaded _ps) ─────────────────────────────────────
        c_caption = _ps["caption"]
        if c_caption:
            try:
                caption = c_caption.format(
                    filename=new_filename,
                    filesize=humanbytes(media.file_size),
                    duration=convert(duration),
                )
            except Exception as e:
                return await _safe_edit(ms, f"❌ <b>Caption error:</b> <code>{e}</code>")
        else:
            caption = f"**{new_filename}**"

        # ── Metadata ──────────────────────────────────────────────────────────
        is_cbz_pdf_upload = upload_type == "cbzpdf"

        if not is_cbz_pdf_upload:
            _bool_metadata  = _ps["metadata"]
            metadata_fields = _ps["metadata_fields"]
            if _bool_metadata:
                _has_meta_vals = any((v or "").strip() for v in metadata_fields.values())
                if _has_meta_vals:
                    result = await add_metadata(file_path, metadata_path, metadata_fields, ms)
                    if not result:
                        _bool_metadata = False
                else:
                    _bool_metadata = False
            else:
                await _safe_edit(ms, "╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n┃  🧬  Evolving metadata...\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯", cancel_kb)
        else:
            await _safe_edit(ms, "📚 <b>Preparing upload…</b>")

        # ── Upload (reference-repo pattern) ──────────────────────────────
        upload_path   = metadata_path if _bool_metadata else file_path
        _bin_sent_man = None
        _REF_LARGE    = 2_090_000_000   # reference threshold: 2090000000 bytes

        await _safe_edit(ms, "╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n┃  ⬆️  Transmitting...\n┃  <code>[██████████]</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯", cancel_kb)

        sent_message = None
        try:
            _ul_c_time = time.time()
            if _file_size > _REF_LARGE and _ub_manual:
                # Large-file: userbot → BIN_CHANNEL, then bot copies to user
                _pa = (job_id, "Uploading", ms, _ul_c_time, cancel_kb)
                if upload_type in ("document", "cbzpdf"):
                    _bin_sent_man = await _ul_client.send_document(
                        Config.BIN_CHANNEL,
                        document=upload_path, file_name=new_filename,
                        thumb=ph_path, caption=caption,
                        progress=_pipeline_progress, progress_args=_pa,
                    )
                elif upload_type == "video":
                    _bin_sent_man = await _ul_client.send_video(
                        Config.BIN_CHANNEL,
                        video=upload_path, file_name=new_filename,
                        caption=caption, thumb=ph_path, duration=duration,
                        progress=_pipeline_progress, progress_args=_pa,
                    )
                else:
                    _bin_sent_man = await _ul_client.send_audio(
                        Config.BIN_CHANNEL,
                        audio=upload_path, file_name=new_filename,
                        caption=caption, thumb=ph_path, duration=duration,
                        progress=_pipeline_progress, progress_args=_pa,
                    )
                await asyncio.sleep(2)   # let Telegram process before copy
                sent_message = await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=_bin_sent_man.chat.id,
                    message_id=_bin_sent_man.id,
                    caption=caption,
                )
                logger.info("[pipeline] job=%s large-file relay  bin=%d  user=%d",
                            job_id, _bin_sent_man.id, sent_message.id)
            else:
                # Small file: bot sends directly to user
                _pa = (job_id, "Uploading", ms, _ul_c_time, cancel_kb)
                if upload_type in ("document", "cbzpdf"):
                    sent_message = await bot.send_document(
                        chat_id, document=upload_path, file_name=new_filename,
                        thumb=ph_path, caption=caption,
                        progress=_pipeline_progress, progress_args=_pa,
                    )
                elif upload_type == "video":
                    sent_message = await bot.send_video(
                        chat_id, video=upload_path, file_name=new_filename,
                        caption=caption, thumb=ph_path, duration=duration,
                        progress=_pipeline_progress, progress_args=_pa,
                    )
                elif upload_type == "audio":
                    sent_message = await bot.send_audio(
                        chat_id, audio=upload_path, file_name=new_filename,
                        caption=caption, thumb=ph_path, duration=duration,
                        progress=_pipeline_progress, progress_args=_pa,
                    )

                if sent_message:
                    _orig_name = getattr(media, "file_name", "") or ""
                    _log_cap   = (
                        f"📂 <b>{_orig_name}</b>\n➜ ✏️ <b>{new_filename}</b>"
                        if _orig_name else f"✏️ <b>{new_filename}</b>"
                    )
                    try:
                        if _bin_sent_man:
                            await _ul_client.edit_message_caption(
                                _bin_sent_man.chat.id, _bin_sent_man.id, caption=_log_cap,
                            )
                        else:
                            await bot.copy_message(
                                chat_id=Config.BIN_CHANNEL,
                                from_chat_id=chat_id,
                                message_id=sent_message.id,
                                caption=_log_cap,
                            )
                    except Exception as _le:
                        logger.warning("BIN_CHANNEL log failed: %s", _le)

        except asyncio.CancelledError:
            await _safe_edit(ms, "🛑 <b>Cancelled.</b>")
            return
        except Exception as e:
            return await _safe_edit(ms, f"❌ <b>Internal Error</b>\nUpload failed: <code>{e}</code>")

        if sent_message:
            if _ps.get("dump_mode") and _ps.get("dump_channel"):
                _orig_name = getattr(media, "file_name", "") or ""
                try:
                    _un = getattr(update.from_user, "username", None) if hasattr(update, "from_user") else None
                    _uname_str = f"@{_un}" if _un else "No Username"
                except Exception:
                    _uname_str = "No Username"
                _dump_size = getattr(media, "file_size", 0) or 0
                asyncio.create_task(
                    _dump_to_channel(
                        bot, user_id, int(_ps["dump_channel"]), sent_message,
                        original_name=_orig_name, new_name=new_filename,
                        username_str=_uname_str, job_id=job_id,
                        file_size=_dump_size,
                    )
                )

        # ── Increment manual daily counter (free limit tracking) ─────────────
        if not _ps.get("premium"):
            asyncio.create_task(jishubotz.inc_manual_rename_today(user_id))

        try:
            from plugins.leaderboard import record_rename, record_history
            display   = update.from_user.first_name if hasattr(update, "from_user") else str(user_id)
            file_sz   = getattr(media, "file_size", 0) or 0
            asyncio.create_task(record_rename(user_id, display))
            asyncio.create_task(record_history(user_id, new_filename, file_sz))
        except Exception:
            pass

        await ms.delete()
        logger.info(
            "✔ PIPELINE DONE   user=%s  filename=%s  job=%s",
            user_id, new_filename, job_id
        )

    finally:
        _manual_tasks.pop(job_id, None)
        # Remove persistent record — job is done (success / error / cancel)
        try:
            await jishubotz.delete_pending_job(job_id)
        except Exception as _dpe:
            logger.warning("[pipeline] delete_pending_job failed job=%s: %s", job_id, _dpe)
        if ph_path:
            _safe_remove(ph_path)
        if _bool_metadata:
            _safe_remove(metadata_path)
        _cleanup_dir(dl_dir)


# ══════════════════════════════════════════════════════════════════════════════
# Progress callback — feeds UI bar AND /status tracker, checks cancel
# ══════════════════════════════════════════════════════════════════════════════

# Per-message last-edit timestamps for throttling
_msg_last_edit: dict[int, float] = {}


_pipeline_progress_prev: dict = {}   # job_id → last bytes seen

async def _pipeline_progress(current: int, total: int, job_id: str, status_label: str, ms, _start, reply_markup=None):
    import math
    from helper.utils import humanbytes, TimeFormatter

    # Bandwidth tracking — synchronous, no task overhead
    prev = _pipeline_progress_prev.get(job_id, 0)
    delta = current - prev
    if delta > 0:
        _pipeline_progress_prev[job_id] = current
        if status_label == "Downloading":
            _rq.record_download(delta)
        else:
            _rq.record_upload(delta)
    if current >= total:
        _pipeline_progress_prev.pop(job_id, None)

    now  = time.time()
    diff = now - _start
    if diff < 0.5:
        return

    speed = current / diff if diff > 0 else 0
    eta_s = (total - current) / speed if speed > 0 else 0

    msg_key = getattr(ms, "id", id(ms))
    last    = _msg_last_edit.get(msg_key, 0)
    if current != total and (now - last) < 8:
        return
    _msg_last_edit[msg_key] = now

    try:
        pct       = current * 100 / total if total > 0 else 0
        filled    = math.floor(pct / 10)
        bar       = "█" * filled + "░" * (10 - filled)
        eta_str   = TimeFormatter(milliseconds=int(eta_s * 1000)) or "0s"
        phase     = "⬇️  Acquiring" if status_label == "Downloading" else "⬆️  Transmitting"
        size_str  = f"{humanbytes(current)} / {humanbytes(total)}"
        speed_str = humanbytes(speed)

        text = (
            f"╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n"
            f"┣━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃  {phase}\n"
            f"┃  <code>[{bar}]</code>  {round(pct, 1)}%\n"
            f"┃  📦  {size_str}\n"
            f"┃  ⚡  {speed_str}/s  ·  ⏱ {eta_str}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

        await ms.edit(text, reply_markup=reply_markup)
    except Exception:
        pass
    finally:
        if current == total:
            _msg_last_edit.pop(msg_key, None)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _safe_edit(ms, text: str, reply_markup=None) -> None:
    try:
        await ms.edit(text, reply_markup=reply_markup)
    except Exception:
        pass


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _cleanup_dir(directory: str) -> None:
    try:
        if not os.path.exists(directory):
            return
        for f in os.listdir(directory):
            _safe_remove(os.path.join(directory, f))
        os.rmdir(directory)
    except Exception:
        pass


async def _dump_to_channel(
    bot,
    user_id: int,
    channel_id: int,
    sent_message,
    original_name: str = "",
    new_name: str = "",
    username_str: str = "",
    job_id: str = "",
    file_size: int = 0,
) -> None:
    """Forward the renamed file to the user's dump channel with rich metadata caption."""
    from helper.utils import humanbytes as _hb

    # Build username display safely
    if not username_str:
        try:
            _chat = await bot.get_chat(user_id)
            _un   = getattr(_chat, "username", None)
            username_str = f"@{_un}" if _un else "No Username"
        except Exception:
            username_str = "No Username"

    # Build caption in the new rich format
    size_str = _hb(file_size) if file_size else "N/A"
    job_str  = f"<code>{job_id}</code>" if job_id else "N/A"
    dump_caption = (
        "╭━━━〔 📂 FILE INFO 〕━━━╮\n\n"
        f"📂 Original:\n<code>{original_name or 'N/A'}</code>\n\n"
        f"➜ ✏️ Renamed:\n<code>{new_name or 'N/A'}</code>\n\n"
        f"👤 User: {username_str}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"🆔 Job: {job_str}\n"
        f"📦 Size: {size_str}\n"
        f"📊 Status: Completed\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯"
    )

    try:
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        mi_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 MediaInfo", callback_data="action_mediainfo"),
        ]])

        # Step 1: copy the file — always works regardless of admin permissions
        dumped = await bot.copy_message(
            chat_id=channel_id,
            from_chat_id=sent_message.chat.id,
            message_id=sent_message.id,
            caption=dump_caption,
        )

        # Step 2: add the MediaInfo button via edit — requires bot to be admin
        # in the channel with "Edit messages" permission. Fails silently if not.
        try:
            await bot.edit_message_reply_markup(
                chat_id=channel_id,
                message_id=dumped.id,
                reply_markup=mi_kb,
            )
        except Exception as _kb_err:
            logger.warning(
                "Dump button not added for channel %s (bot may need Edit Messages permission): %s",
                channel_id, _kb_err,
            )

        # Register in _file_cache so MediaInfo callback can resolve the file
        _file_cache[dumped.id] = sent_message

        logger.info("Dumped to channel %s for user %s job=%s", channel_id, user_id, job_id)
    except Exception as e:
        logger.error("Dump failed for user %s → channel %s: %s", user_id, channel_id, e)
        try:
            await bot.send_message(
                user_id,
                f"⚠️ Could not dump to channel `{channel_id}`: `{e}`",
                disable_notification=True,
            )
        except Exception:
            pass


def _fmt_dur(ms: int) -> str:
    s, ms = divmod(ms, 1000)
    m, s  = divmod(s, 60)
    h, m  = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _fmt_br(br) -> str:
    try:
        br = int(br)
        if br >= 1_000_000:
            return f"{br / 1_000_000:.2f} Mbps"
        return f"{br / 1_000:.0f} Kbps"
    except Exception:
        return str(br)
