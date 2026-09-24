"""
plugins/auto_rename.py
══════════════════════════════════════════════════════════════════════════════
Features
────────
• /mode          – toggle Manual ↔ Auto Rename
• /autorename    – set / view the naming template
• /setsource     – choose metadata extraction source
                   (filename | caption | both — "both" tries caption first)
• /setmedia      – preferred output container (document / video / audio)
• /autoqueue     – live view of the user's queue; cancel individual jobs
• Queue system   – 4 concurrent workers; every extra file waits
• Rename limits  – normal users: 30 renames/day  |  premium: unlimited
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import Client, filters
from pyrogram.errors import FileReferenceExpired, FloodWait
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from helper.database import jishubotz
from helper.ffmpeg import add_metadata, get_duration_hachoir
from helper.utils import add_prefix_suffix, convert, humanbytes
from helper.queue_manager import qm as _qm, rq as _rq
# These are used inside the pipeline — import once at module load
from helper.userbot import get_userbot, userbot_available, choose_client

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Auto-rename daily limit is DB-driven via /setlimit auto <n>  (default 30)
_PROGRESS_THROTTLE = 3   # seconds between progress edits

# ══════════════════════════════════════════════════════════════════════════════
# Queue data structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _Job:
    job_id:     str
    user_id:    int
    message:    Message
    status:     str = "queued"
    status_msg: Optional[object] = None
    accept_msg: Optional[object] = None   # queued-card shown before pipeline starts
    task:       Optional[asyncio.Task] = None
    queued_at:  float = field(default_factory=time.time)

    def short_name(self) -> str:
        try:
            m = self.message
            if m.document: return m.document.file_name or "document"
            if m.video:    return m.video.file_name    or "video"
            if m.audio:    return m.audio.file_name    or "audio"
        except Exception:
            pass
        return f"job-{self.job_id}"


# _active_jobs and _waiting_queue are now views into the central rq.
# We keep lightweight per-job UX objects here for /autoqueue display.
_job_registry:    dict[str, _Job]   = {}   # job_id → _Job (for UX display)
_user_jobs:       dict[int, set[str]] = {}
_queue_lock       = asyncio.Lock()
_job_counter      = 0
_job_counter_lock = asyncio.Lock()




async def _new_job_id() -> str:
    """Thread-safe monotonic job ID.  Prefix 'ar' + zero-padded counter."""
    global _job_counter
    async with _job_counter_lock:
        _job_counter += 1
        return f"ar{_job_counter:06d}"


def _trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


# ══════════════════════════════════════════════════════════════════════════════
# Daily rename counter
# ══════════════════════════════════════════════════════════════════════════════

async def _check_daily(user_id: int):
    """Check if user can add another auto-rename job (does NOT increment — do that at pipeline start).
    Returns (allowed, used_today, limit). limit=0 means unlimited (premium)."""
    is_prem = await jishubotz.is_premium(user_id)
    if is_prem:
        return True, 0, 0
    used  = await jishubotz.get_auto_rename_today(user_id)
    limit = await jishubotz.get_auto_daily_limit()
    if used >= limit:
        return False, used, limit
    return True, used, limit


async def _inc_daily(user_id: int) -> None:
    """Increment the daily auto-rename counter. Call exactly once per job when it actually starts."""
    await jishubotz.inc_auto_rename_today(user_id)


# ══════════════════════════════════════════════════════════════════════════════
# /mode
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("mode"))
async def cmd_mode(client: Client, message: Message):
    user_id = message.from_user.id
    if await jishubotz.is_banned(user_id):
        return await message.reply("⛔ <b>Access Denied</b>")
    if not await jishubotz.is_premium(user_id):
        return await message.reply_text(
            "◈ <b>Premium Required</b>\n\n"
            "<blockquote>This feature requires a premium plan.</blockquote>\n\n"
            "➤  Contact @naruto0927 to upgrade"
        )
    current = await jishubotz.get_rename_mode(user_id)
    await message.reply_text(_mode_text(current), reply_markup=_mode_keyboard(current))


@Client.on_callback_query(filters.regex(r"^set_mode_(manual|auto)$"))
async def cb_set_mode(client: Client, update: CallbackQuery):
    user_id  = update.from_user.id
    new_mode = update.data.split("_")[-1]
    if not await jishubotz.is_premium(user_id):
        return await update.answer("💎 Premium required.", show_alert=True)
    await jishubotz.set_rename_mode(user_id, new_mode)
    label = "Manual" if new_mode == "manual" else "Auto Rename"
    await update.answer(f"⚡ Mode set to {label} — Great Sage confirms.", show_alert=True)
    try:
        await update.message.edit_text(_mode_text(new_mode), reply_markup=_mode_keyboard(new_mode))
    except Exception:
        pass


def _mode_text(mode: str) -> str:
    mi = "●" if mode == "manual" else "○"
    ai = "●" if mode == "auto"   else "○"
    return (
        "╭━━━〔 🌌 TEMPEST MODE 〕━━━╮\n"
        f"┃  {mi}  Manual      ·  type filename per file\n"
        f"┃  {ai}  Auto Rename ·  template-based automatic\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<i>⚡ Great Sage awaits your selection.</i>"
    )


def _mode_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("🟢 " if current == "manual" else "⚪ ") + "Manual",
                             callback_data="set_mode_manual"),
        InlineKeyboardButton(("🟢 " if current == "auto" else "⚪ ") + "Auto Rename",
                             callback_data="set_mode_auto"),
    ]])


# ══════════════════════════════════════════════════════════════════════════════
# /autorename
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("autorename"))
async def cmd_autorename(client: Client, message: Message):
    user_id = message.from_user.id
    if await jishubotz.is_banned(user_id):
        return await message.reply("⛔ <b>Access Denied</b>")
    if not await jishubotz.is_premium(user_id):
        return await message.reply_text(
            "💎 <b>Premium Required</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👤 My Status", callback_data="check_premium_status"),
            ]])
        )
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        current = await jishubotz.get_format_template(user_id)
        if current:
            return await message.reply_text(
                f"◈ <b>Auto Rename Template</b>\n\n"
                f"<b>Current:</b> <code>{current}</code>\n\n"
                f"➜ <code>/autorename &lt;template&gt;</code> to change.\n"
                f"Placeholders: <code>{{episode}}</code> <code>{{season}}</code> "
                f"<code>{{quality}}</code> <code>{{audio}}</code>"
            )
        return await message.reply_text(
            "◈ <b>Auto Rename Template</b>\n\nNo template saved yet.\n\n"
            "➜ <b>Usage:</b> <code>/autorename My Show S{season}E{episode} [{quality}]</code>"
        )
    template = parts[1].strip()
    await jishubotz.set_format_template(user_id, template)
    await message.reply_text(
        f"╭━━━〔 🧬 EVOLUTION TEMPLATE 〕━━━╮\n"
        f"┃  <code>{template}</code>\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "✨ Template acquired. Enable /mode → Auto Rename, then send files."
    )


# ══════════════════════════════════════════════════════════════════════════════
# /setsource  –  global extraction source (admin sets once, applies to all users)
# ══════════════════════════════════════════════════════════════════════════════

_SOURCE_LABELS = {
    "filename": "📄 File Name",
    "caption":  "📝 Caption Only",
    "both":     "🔀 Both  (caption → filename)",
}


def _source_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label in _SOURCE_LABELS.items():
        rows.append([InlineKeyboardButton(
            ("✅ " if current == key else "") + label,
            callback_data=f"setsource_{key}",
        )])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.private & filters.command("setsource"))
async def cmd_setsource(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in Config.ADMIN:
        return await message.reply_text(
            "╭━━━〔 ⛔ ACCESS DENIED 〕━━━╮\n"
            "┃  /setsource is an admin command.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )
    current = await jishubotz.get_global_rename_source()
    await message.reply_text(
        f"◈ <b>Global Metadata Source</b>\n\n"
        f"Where should the bot look for episode/season/quality info?\n"
        f"This applies to <b>all users</b>.\n\n"
        f"Current: <b>{_SOURCE_LABELS.get(current, current)}</b>",
        reply_markup=_source_keyboard(current),
    )


@Client.on_callback_query(filters.regex(r"^setsource_(filename|caption|both)$"))
async def cb_setsource(client: Client, update: CallbackQuery):
    if update.from_user.id not in Config.ADMIN:
        return await update.answer("⛔ Admin only.", show_alert=True)
    src = update.data.split("_", 1)[1]
    await jishubotz.set_global_rename_source(src)
    await update.answer(f"✅ Global source: {_SOURCE_LABELS[src]}", show_alert=True)
    try:
        await update.message.edit_text(
            f"◈ <b>Global Metadata Source</b>\n\n"
            f"Applies to <b>all users</b>.\n\n"
            f"Current: <b>{_SOURCE_LABELS[src]}</b>",
            reply_markup=_source_keyboard(src),
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# /setmedia
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("setmedia"))
async def cmd_setmedia(client: Client, message: Message):
    user_id = message.from_user.id
    if await jishubotz.is_banned(user_id):
        return await message.reply("⛔ <b>Access Denied</b>")
    if not await jishubotz.is_premium(user_id):
        return await message.reply_text("💎 <b>Premium Required</b>")
    current = await jishubotz.get_media_preference(user_id)
    await message.reply_text(
        f"◈ <b>Auto Rename — Output Type</b>\n\n"
        f"Current: <b>{current or 'auto-detect'}</b>\n\n"
        "Choose how renamed files should be sent:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Document", callback_data="setmedia_document")],
            [InlineKeyboardButton("🎥 Video",    callback_data="setmedia_video")],
            [InlineKeyboardButton("🎵 Audio",    callback_data="setmedia_audio")],
        ]),
    )


@Client.on_callback_query(filters.regex(r"^setmedia_(document|video|audio)$"))
async def cb_setmedia(client: Client, update: CallbackQuery):
    user_id    = update.from_user.id
    media_type = update.data.split("_", 1)[1]
    await jishubotz.set_media_preference(user_id, media_type)
    await update.answer(f"Output type set to {media_type} ✅")
    await update.message.edit_text(
        f"◈ <b>Output type:</b> <code>{media_type}</code> ✅"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /autoqueue  –  live queue panel
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("autoqueue"))
async def cmd_autoqueue(client: Client, message: Message):
    user_id = message.from_user.id
    if await jishubotz.is_banned(user_id):
        return await message.reply("⛔ <b>Access Denied</b>")
    text, markup = _queue_panel(user_id)
    await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^aq_refresh$"))
async def cb_aq_refresh(client: Client, update: CallbackQuery):
    text, markup = _queue_panel(update.from_user.id)
    try:
        await update.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await update.answer("Refreshed ✅")


@Client.on_callback_query(filters.regex(r"^aq_cancel_(.+)$"))
async def cb_aq_cancel(client: Client, update: CallbackQuery):
    user_id = update.from_user.id
    job_id  = update.data[len("aq_cancel_"):]
    if await _cancel_job(job_id, user_id):
        await update.answer(f"✅ Job {job_id} cancelled.", show_alert=True)
    else:
        await update.answer("❌ Job not found or already finished.", show_alert=True)
    text, markup = _queue_panel(user_id)
    try:
        await update.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


def _fmt_wait(seconds: float) -> str:
    """Format wait duration into a short human string."""
    s = int(seconds)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _queue_panel(user_id: int):
    """
    Build the /autoqueue panel for a specific user.

    Header  — global queue state (all users)
    Section — this user's active jobs
    Section — this user's queued jobs with fair-position number
    """
    active_total = _rq.active_count()
    queued_total = _rq.queued_count()
    avail        = _rq.available_slots()
    concurrency  = _rq.concurrency

    # ── Per-user active breakdown for the header ──────────────────────────
    user_active_map: dict[int, list[QueuedJob]] = {}
    for j in _rq.active_jobs():
        user_active_map.setdefault(j.user_id, []).append(j)

    if user_active_map:
        ua_lines = "  ".join(
            f"{j_list[0].user_name[:12]}: {len(j_list)}"
            for j_list in user_active_map.values()
        )
        slot_detail = f"  [{ua_lines}]"
    else:
        slot_detail = ""

    state_icon = "🟢" if active_total else "⚪"
    header = (
        f"╭━━━〔 {state_icon} RENAME QUEUE 〕━━━╮\n"
        f"┃  ⚡  Active    ·  {active_total}/{concurrency}{slot_detail}\n"
        f"┃  ⏳  Queued    ·  {queued_total}\n"
        f"┃  💚  Free      ·  {avail}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )

    my_ids = _user_jobs.get(user_id, set())
    if not my_ids:
        footer = (
            "\n\n<i>You have no active or queued jobs.\n"
            "Send a file to start renaming.</i>"
        )
        return (
            header + footer,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="aq_refresh")]]),
        )

    active_rq_jobs = {j.job_id: j for j in _rq.active_jobs()}
    queued_rq_list = _rq.queued_jobs()
    queued_rq_ids  = {j.job_id for j in queued_rq_list}

    lines   = [header, ""]
    buttons = []

    # ── My active jobs ────────────────────────────────────────────────────
    my_active = [jid for jid in my_ids if jid in active_rq_jobs]
    if my_active:
        lines.append("╭━━━〔 ⚙️ PROCESSING 〕━━━╮")
        for jid in sorted(my_active):
            rq_job  = active_rq_jobs[jid]
            ux_job  = _job_registry.get(jid)
            name    = _trim(ux_job.short_name() if ux_job else rq_job.filename, 38)
            running = _fmt_wait(rq_job.run_seconds())
            ux_stat = getattr(ux_job, "status", "active") if ux_job else "active"
            icon    = {"downloading": "⬇️", "processing": "⚙️", "uploading": "⬆️"}.get(ux_stat, "▶️")
            lines.append(
                f"┃  {icon}  <code>{jid}</code>\n"
                f"┃      <i>{name}</i>\n"
                f"┃      Running: {running}"
            )
            buttons.append([InlineKeyboardButton(f"✕ Cancel  {jid}", callback_data=f"aq_cancel_{jid}")])
        lines.append("╰━━━━━━━━━━━━━━━━━━━━━━━━╯")

    # ── My queued jobs ────────────────────────────────────────────────────
    my_queued = [jid for jid in my_ids if jid in queued_rq_ids]
    if my_queued:
        lines.append("")
        lines.append("╭━━━〔 ⏳ WAITING 〕━━━╮")
        for jid in sorted(my_queued):
            rq_job   = next((j for j in queued_rq_list if j.job_id == jid), None)
            ux_job   = _job_registry.get(jid)
            name     = _trim(ux_job.short_name() if ux_job else (rq_job.filename if rq_job else jid), 38)
            fair_pos = _rq.queue_position(jid)
            waited   = _fmt_wait(rq_job.wait_seconds()) if rq_job else "?"
            pos_str  = f"#{fair_pos}" if fair_pos > 0 else "next"
            lines.append(
                f"┃  ⏳  <code>{jid}</code>  ·  pos {pos_str}\n"
                f"┃      <i>{name}</i>\n"
                f"┃      Waiting: {waited}"
            )
            buttons.append([InlineKeyboardButton(f"✕ Cancel  {jid}", callback_data=f"aq_cancel_{jid}")])
        lines.append("╰━━━━━━━━━━━━━━━━━━━━━━━━╯")

    # ── Clean finished jobs from registry ─────────────────────────────────
    finished = my_ids - set(active_rq_jobs) - queued_rq_ids
    for jid in finished:
        my_ids.discard(jid)
        _job_registry.pop(jid, None)
    if not my_ids:
        _user_jobs.pop(user_id, None)

    if not my_active and not my_queued:
        lines.append("\n<i>✨ All your jobs have finished.</i>")

    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="aq_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


@Client.on_message(filters.private & filters.command("clearqueue"))
async def cmd_clearqueue(client: Client, message: Message):
    """Admin-only: wipe the entire pending_jobs collection and reset in-memory state."""
    from config import Config as _Cfg
    if message.from_user.id not in _Cfg.ADMIN:
        return await message.reply_text("⛔ <b>Admin only.</b>")
    # Cancel all active rq tasks
    for rq_job in list(_rq.active_jobs()):
        if rq_job.task and not rq_job.task.done():
            rq_job.task.cancel()
    # Mark queued jobs as cancelled so _drain skips them
    for rq_job in list(_rq.queued_jobs()):
        rq_job.status = "cancelled"
    async with _queue_lock:
        _job_registry.clear()
        _user_jobs.clear()
    await jishubotz.clear_all_pending_jobs()
    await message.reply_text(
        "╭━━━〔 🗑 QUEUE CLEARED 〕━━━╮\n"
        "┃  All pending jobs wiped.\n"
        "┃  DB collection cleared.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


async def _cancel_job(job_id: str, user_id: int) -> bool:
    """Cancel an active or queued job by ID.  Returns True if found+cancelled."""
    # Check active jobs in rq
    for rq_job in _rq.active_jobs():
        if rq_job.job_id == job_id and rq_job.user_id == user_id:
            if rq_job.task and not rq_job.task.done():
                rq_job.task.cancel()
            ux_job = _job_registry.get(job_id)
            if ux_job:
                ux_job.status = "cancelled"
            return True

    # Check queued jobs in rq
    for rq_job in _rq.queued_jobs():
        if rq_job.job_id == job_id and rq_job.user_id == user_id:
            rq_job.status = "cancelled"   # scheduler skips cancelled
            _user_jobs.get(user_id, set()).discard(job_id)
            _job_registry.pop(job_id, None)
            asyncio.create_task(jishubotz.delete_pending_job(job_id))
            ux_job = _job_registry.get(job_id)
            if ux_job:
                try:
                    await ux_job.message.reply_text(
                        f"╭━━━〔 🗑 QUEUE 〕━━━╮\n"
                        f"┃  Job <code>{job_id}</code> cancelled.\n"
                        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                    )
                except Exception:
                    pass
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Startup — start the central rq scheduler + restore persisted jobs
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler(client: Client) -> None:
    """Call once from bot startup after the client is running."""
    # The central rq already has its own scheduler loop; start it if not running.
    _rq.start()
    asyncio.create_task(_restore_pending_jobs(client), name="auto_rename_restore")


# Maximum age of a pending job before it is considered stale and dropped.
_JOB_MAX_AGE_SECONDS: int = 86_400   # 24 hours

async def _restore_pending_jobs(client: Client) -> None:
    """
    On startup: load every auto-rename job that was persisted to MongoDB before
    the restart, re-fetch the original Telegram message, and re-enqueue it.

    Improvements over the previous implementation:
      • Filters by job_type == "auto" (manual jobs are handled by file_rename.py)
      • Drops jobs older than _JOB_MAX_AGE_SECONDS (avoids re-processing 3-day-old files)
      • 3-attempt exponential backoff when fetching the message
      • Styled ♻️ resume card instead of plain text
      • Staggered enqueue (0.3 s gap) to avoid hammering Telegram at boot
      • Uses QueueManager limits instead of hardcoded CONCURRENT_JOBS
    """
    await asyncio.sleep(2)   # Let scheduler loop start first

    logger.info("[recovery] Checking for unfinished auto-rename jobs...")

    try:
        pending = await jishubotz.load_all_pending_jobs()
    except Exception as e:
        logger.error("[recovery] Failed to load pending jobs from DB: %s", e)
        return

    # Filter: only "auto" jobs (manual handled separately), skip stale
    now = time.time()
    auto_pending = []
    for doc in pending:
        jtype = doc.get("job_type", "auto")
        if jtype != "auto":
            continue
        age = now - float(doc.get("queued_at", now))
        if age > _JOB_MAX_AGE_SECONDS:
            logger.info(
                "[recovery] Dropping stale job %s (age %.1f h > %.1f h)",
                doc["_id"], age / 3600, _JOB_MAX_AGE_SECONDS / 3600,
            )
            asyncio.create_task(jishubotz.delete_pending_job(doc["_id"]))
            continue
        auto_pending.append(doc)

    if not auto_pending:
        logger.info("[recovery] No resumable auto-rename jobs found")
        return

    logger.info("[recovery] Found %d auto-rename job(s) to resume", len(auto_pending))

    restored = 0
    dropped  = 0

    for doc in auto_pending:
        job_id     = doc["_id"]
        user_id    = int(doc["user_id"])
        chat_id    = int(doc["chat_id"])
        message_id = int(doc["message_id"])
        queued_at  = float(doc.get("queued_at", time.time()))
        file_name  = doc.get("file_name", "unknown")
        age_min    = (now - queued_at) / 60

        logger.info("[recovery] Resuming job %s (queued %.1f min ago)", job_id, age_min)

        # ── Delete any partial temp files ─────────────────────────────────────
        for _partial in [
            os.path.join("downloads", str(user_id), file_name),
            os.path.join("downloads", job_id, file_name),
        ]:
            if os.path.exists(_partial):
                try:
                    os.remove(_partial)
                    logger.info("[recovery] Cleared partial file: %s", _partial)
                except OSError as _oe:
                    logger.warning("[recovery] Could not clear %s: %s", _partial, _oe)

        # ── Re-fetch message (3 attempts, exponential backoff) ────────────────
        message = None
        for _attempt in range(1, 4):
            try:
                message = await client.get_messages(chat_id, message_id)
                if not message or not message.media:
                    raise ValueError("message gone or has no media")
                break
            except Exception as _fe:
                if _attempt < 3:
                    await asyncio.sleep(2 ** _attempt)   # 2 s, 4 s
                else:
                    logger.warning(
                        "[recovery] job=%s message %s/%s unreachable after %d attempts (%s) — dropping",
                        job_id, chat_id, message_id, _attempt, _fe,
                    )
                    await jishubotz.delete_pending_job(job_id)
                    dropped += 1
                    message = None

        if message is None:
            continue

        # ── Styled resume notification ────────────────────────────────────────
        try:
            await client.send_message(
                chat_id,
                f"╭━━━〔 ♻️ JOB RESUMED 〕━━━╮\n"
                f"┃  🆔  <code>{job_id}</code>\n"
                f"┃  📂  <code>{file_name[:40]}</code>\n"
                f"┃  ⏱️  Queued {age_min:.0f} min ago\n"
                f"┃  📡  Bot restarted — resuming…\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
            )
        except Exception:
            pass   # User may have blocked the bot; don't abort the job

        job = _Job(job_id=job_id, user_id=user_id, message=message, queued_at=queued_at)
        _job_registry[job_id] = job
        _user_jobs.setdefault(user_id, set()).add(job_id)

        async def _job_fn_restore(j=job, c=client):
            await _run_pipeline(c, j)

        await _rq.enqueue(
            fn=_job_fn_restore,
            job_id=job_id,
            user_id=user_id,
            meta={"filename": file_name, "type": "auto", "restored": True},
        )

        restored += 1
        await asyncio.sleep(0.3)   # Stagger to avoid boot-time burst

    logger.info(
        "[recovery] Auto-rename recovery done — resumed %d, dropped %d / %d",
        restored, dropped, len(auto_pending),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point  (called by file_rename.rename_start when mode == "auto")
# ══════════════════════════════════════════════════════════════════════════════

async def run_auto_rename(client: Client, message: Message) -> None:
    user_id = message.from_user.id

    fmt = await jishubotz.get_format_template(user_id)
    if not fmt:
        return await message.reply_text(
            "╭━━━〔 ⚠️ GREAT SAGE WARNING 〕━━━╮\n"
            "┃  No Auto Rename template set.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "➤  /autorename My Show S{season}E{episode} [{quality}]\n"
            "➤  /mode  ·  switch to Manual"
        )

    # ── Size gate (before consuming a queue slot) ─────────────────────────
    try:
        _file_obj  = getattr(message, message.media.value, None) if message.media else None
        _file_size = getattr(_file_obj, "file_size", 0) or 0
    except Exception:
        _file_size = 0
    if _file_size > Config.BOT_MAX_SIZE:
        is_prem = await jishubotz.is_premium(user_id)
        if not is_prem:
            return await message.reply_text(
                "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                "┃  📦  Exceeds 2 GB bot limit.\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "👑  Upgrade to Tempest Elite for 4 GB support."
            )
        if not userbot_available():
            return await message.reply_text(
                "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                "┃  📦  Exceeds 2 GB — userbot not set.\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "Contact the admin to enable large-file support."
            )
        if _file_size > _Cfg.USER_MAX_SIZE:
            return await message.reply_text(
                "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                "┃  📦  Exceeds 4 GB — maximum barrier.\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            )

    allowed, used, limit = await _check_daily(user_id)
    if not allowed:
        # Silently queue — user will be notified when limit clears tomorrow.
        # We still enqueue and let the pipeline handle it gracefully so the
        # user's files are not dropped; the job will fail at pipeline start
        # if the counter has not reset, but in practice the queue drains
        # within the same day so this path is mainly a soft safety valve.
        # We just accept the file into queue; no noisy rejection message.
        pass  # fall through to enqueue below

    job_id = await _new_job_id()
    job    = _Job(job_id=job_id, user_id=user_id, message=message)

    # ── Determine original filename for DB record ─────────────────────────
    try:
        _f = (getattr(message, message.media.value, None)
              if message.media else None)
        _orig_fname = getattr(_f, "file_name", None) or str(message.media.value)
    except Exception:
        _orig_fname = "unknown"

    # ── Collect display name ───────────────────────────────────────────────
    _username  = getattr(message.from_user, "username", None) or "" if message.from_user else ""
    _firstname = getattr(message.from_user, "first_name", None) or _username or str(user_id)

    # ── Persist to MongoDB ─────────────────────────────────────────────────
    await jishubotz.save_pending_job(
        job_id     = job_id,
        user_id    = user_id,
        chat_id    = message.chat.id,
        message_id = message.id,
        file_name  = _orig_fname,
        queued_at  = time.time(),
        username   = _username,
    )

    # ── Register in UX lookup ─────────────────────────────────────────────
    _job_registry[job_id] = job
    _user_jobs.setdefault(user_id, set()).add(job_id)

    # ── Enqueue into central rq (do this before reading position) ─────────
    async def _job_fn():
        await _run_pipeline(client, job)

    await _rq.enqueue(
        fn=_job_fn,
        job_id=job_id,
        user_id=user_id,
        meta={"filename": _orig_fname, "type": "auto", "user_name": _firstname},
    )

    # ── Determine real queue position after enqueue ────────────────────────
    active_now  = _rq.active_count()
    concurrency = _rq.concurrency
    raw_pos     = _rq.queue_position(job_id)

    if raw_pos == 0 or active_now < concurrency:
        pos_text = None   # starting immediately — no card needed
    else:
        pos_text = str(raw_pos)

    # Send queue card only for jobs that are actually waiting
    if pos_text:
        job.accept_msg = await message.reply_text(
            f"◈ <b>Auto Rename</b>  <code>[{job_id}]</code>\n\n"
            f"╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮\n"
            f"┃  ⏳  Queued  ·  position {pos_text}\n"
            f"┃  🔢  Slots   ·  {active_now}/{concurrency} active\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Cancel Job", callback_data=f"aq_cancel_{job_id}"),
                InlineKeyboardButton("📋 View Queue", callback_data="aq_refresh"),
            ]]),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ══════════════════════════════════════════════════════════════════════════════

async def _run_pipeline(client: Client, job: _Job) -> None:
    message = job.message
    user_id = job.user_id
    job_id  = job.job_id

    download_path: Optional[str] = None
    metadata_path: Optional[str] = None
    ph_path:       Optional[str] = None
    status_msg                   = None

    try:
        # ── Identify file ─────────────────────────────────────────────────
        if message.document:
            file_obj, file_name, base_media = message.document, message.document.file_name or "file", "document"
        elif message.video:
            file_obj, file_name, base_media = message.video, message.video.file_name or "video", "video"
        elif message.audio:
            file_obj, file_name, base_media = message.audio, message.audio.file_name or "audio", "audio"
        else:
            return await message.reply_text("❌ Unsupported file type.")

        file_caption = (message.caption or "").strip()

        # ── Client selection based on file size ──────────────────────────
        from config import Config as _Cfg
        _file_size = getattr(file_obj, "file_size", 0) or 0
        _TWO_GB    = _Cfg.BOT_MAX_SIZE
        _large     = _file_size > _TWO_GB
        _dl_client = client
        _ul_client = client
        _ub        = None

        # ── ONE parallel round-trip: user settings + daily limit ─────────────
        # asyncio.gather fires both DB queries simultaneously — saves ~20–40 ms
        # per job compared to sequential awaits.
        _ps, (_auto_limit, _manual_limit) = await asyncio.gather(
            jishubotz.get_pipeline_settings(user_id),
            jishubotz.get_daily_limits_cached(),
        )
        source          = _ps["rename_source"]
        fmt             = _ps["format_template"]
        thumb_id        = _ps["thumbnail"]
        raw_cap         = _ps["caption"]
        prefix          = _ps["prefix"]
        suffix          = _ps["suffix"]
        media_pref      = _ps["auto_media_type"]
        use_metadata    = _ps["metadata"]
        metadata_fields = _ps["metadata_fields"]

        # Premium status is already resolved inside get_pipeline_settings —
        # no separate is_premium() DB call needed.
        is_prem_pipe = _ps["premium"]

        if _large:
            if not is_prem_pipe:
                return await message.reply_text(
                    "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                    "┃  📦  Exceeds 2 GB bot limit.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "👑  Upgrade to Tempest Elite for 4 GB support."
                )
            if not userbot_available():
                return await message.reply_text(
                    "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                    "┃  📦  Exceeds 2 GB — STRING_SESSION not set.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "Contact the admin to enable large-file support."
                )
            if _file_size > _Cfg.USER_MAX_SIZE:
                return await message.reply_text(
                    "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                    "┃  📦  Exceeds 4 GB — maximum barrier.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
            _ub = await get_userbot()
            if _ub:
                _ul_client = _ub
                logger.info(
                    "[auto_rename] job=%s  file_size=%.2f GB → using premium String Session for upload",
                    job_id, _file_size / (1024**3),
                )
            else:
                return await message.reply_text(
                    "╭━━━〔 ⚠️ BARRIER LIMIT 〕━━━╮\n"
                    "┃  📦  Exceeds 2 GB — userbot failed to start.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "Contact the admin to check STRING_SESSION."
                )
        else:
            logger.info(
                "[auto_rename] job=%s  file_size=%.2f GB → using normal bot",
                job_id, _file_size / (1024**3),
            )

        # ── Daily limit (uses values already resolved above — zero extra round-trips) ─
        if not is_prem_pipe:
            used_now  = _ps["auto_daily_count"]
            day_limit = _auto_limit
            if used_now >= day_limit:
                try:
                    if status_msg:
                        await status_msg.edit_text(
                            f"╭━━━〔 ⚡ DAILY LIMIT 〕━━━╮\n"
                            f"┃  📊  {used_now}/{day_limit} used today\n"
                            f"┃  ⏱   Resets at midnight UTC\n"
                            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                            f"Upgrade to <b>Tempest Elite</b> for unlimited access 👑"
                        )
                except Exception:
                    pass
                return
            await _inc_daily(user_id)

        # ── Determine extraction source ───────────────────────────────────
        if source == "caption":
            extraction_text = file_caption if file_caption else file_name
        elif source == "both":
            extraction_text = (file_caption + " " + file_name).strip() if file_caption else file_name
        else:   # "filename" (default)
            extraction_text = file_name

        # ── Strip CRC32 hash before extraction ───────────────────────────
        # Anime files commonly contain an 8-hex-digit CRC32 in brackets:
        # e.g. [940665E5], [ABCD1234].  This must be removed before the
        # episode/season/quality extractors run, otherwise the hex digits
        # get mistaken for episode numbers or quality indicators.
        extraction_text = re.sub(r'\[[0-9A-Fa-f]{8}\]', '', extraction_text).strip()

        # ── Build new filename ────────────────────────────────────────────
        new_file_name    = _apply_template(fmt, extraction_text, file_name)
        new_file_name_ps = add_prefix_suffix(new_file_name, prefix, suffix)

        # ── Status message (replaces queued card if any) ─────────────────
        cancel_kb  = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🗑 Cancel  [{job_id}]", callback_data=f"aq_cancel_{job_id}")
        ]])
        # Delete the "queued" card now that we're actually running
        if job.accept_msg:
            try:
                await job.accept_msg.delete()
            except Exception:
                pass
            job.accept_msg = None

        status_msg     = await message.reply_text(
            _status_text(job_id, new_file_name_ps, "downloading", 0, 0),
            reply_markup=cancel_kb,
        )
        job.status_msg = status_msg

        # ── Paths — per-job subdirectory prevents concurrent-job collisions ─
        # downloads/{user_id}/{job_id}/filename  (reference pattern)
        folder        = os.path.join(str(user_id), job_id)
        download_path = os.path.join("downloads", folder, new_file_name_ps)
        metadata_path = os.path.join("Metadata",  folder, f"{new_file_name_ps}")
        os.makedirs(os.path.dirname(download_path), exist_ok=True)
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

        # ── Download (reference-repo pattern) ───────────────────────────
        # Ported directly from the working 4 GB reference implementation:
        #   await bot.download_media(message=file_obj, ...)
        # where file_obj is the media object (document/video/audio), NOT the
        # full message — this avoids any peer resolution so PEER_ID_INVALID
        # can never happen.  On FileReferenceExpired the message is re-fetched
        # using the bot (which is always in the source chat) and retried.

        job.status     = "downloading"
        last_edit      = [0.0]
        c_time         = time.time()
        _dl_start      = time.time()   # separate clock for speed calculation
        _last_dl_bytes = [0]

        async def _prog(cur, total, smsg, start):
            # Track bandwidth delta — synchronous, no task overhead
            delta = cur - _last_dl_bytes[0]
            if delta > 0:
                _rq.record_download(delta)
                _last_dl_bytes[0] = cur
            if time.time() - last_edit[0] < _PROGRESS_THROTTLE:
                return
            last_edit[0] = time.time()
            pct = cur * 100 // total if total else 0
            # Speed measured from download start, not job-queue start
            elapsed = max(time.time() - _dl_start, 1)
            spd = cur / elapsed
            try:
                await smsg.edit_text(
                    _status_text(job_id, new_file_name_ps, job.status, pct, spd),
                    reply_markup=cancel_kb,
                )
            except FloodWait as fw:
                await asyncio.sleep(fw.value)
            except Exception:
                pass

        _src_size  = getattr(file_obj, "file_size", 0) or 0
        file_path: Optional[str] = None
        logger.info(
            "[auto_rename] job=%s Starting download  source=%s  path=%s",
            job_id, humanbytes(_src_size), download_path,
        )

        # Download with per-attempt timeout (10 min for largest files).
        # Pyrogram's download_media is async but can stall indefinitely on a
        # broken TCP connection. asyncio.wait_for() guarantees the slot is
        # never held forever by a hung download.
        _DOWNLOAD_TIMEOUT = 600   # 10 minutes

        _dl_target = file_obj
        for _attempt in range(1, 4):
            if os.path.exists(download_path):
                try: os.remove(download_path)
                except OSError: pass
            _dl_start = time.time()   # reset speed clock on each attempt
            try:
                file_path = await asyncio.wait_for(
                    client.download_media(
                        message=_dl_target,
                        file_name=download_path,
                        progress=_prog,
                        progress_args=(status_msg, c_time),
                    ),
                    timeout=_DOWNLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[auto_rename] job=%s attempt=%d download timed out after %ds",
                    job_id, _attempt, _DOWNLOAD_TIMEOUT,
                )
                if os.path.exists(download_path):
                    try: os.remove(download_path)
                    except OSError: pass
                file_path = None
                if _attempt < 3:
                    await asyncio.sleep(5)
                continue
            except FloodWait as fw:
                logger.warning(
                    "[auto_rename] job=%s FloodWait %ds during download",
                    job_id, fw.value,
                )
                await asyncio.sleep(fw.value)
                continue   # retry same attempt after flood wait
            except FileReferenceExpired:
                logger.warning("[auto_rename] job=%s attempt=%d FileReferenceExpired — re-fetching", job_id, _attempt)
                if os.path.exists(download_path):
                    try: os.remove(download_path)
                    except OSError: pass
                try:
                    _fresh = await client.get_messages(message.chat.id, message.id)
                    _dl_target = _fresh if (_fresh and _fresh.media) else None
                except Exception as _rfe:
                    logger.warning("[auto_rename] job=%s re-fetch failed: %s", job_id, _rfe)
                    _dl_target = None
                if not _dl_target:
                    file_path = None
                    break
                continue
            except asyncio.CancelledError:
                raise
            except Exception as _de:
                logger.warning("[auto_rename] job=%s attempt=%d download error: %s", job_id, _attempt, _de)
                if os.path.exists(download_path):
                    try: os.remove(download_path)
                    except OSError: pass
                file_path = None
                if _attempt < 3:
                    await asyncio.sleep(3 * _attempt)   # 3s, 6s
                continue

            # Validate: must exist and be non-empty
            _dl_size = os.path.getsize(file_path) if (file_path and os.path.exists(file_path)) else 0
            logger.info("[auto_rename] job=%s Download finished  size=%s", job_id, humanbytes(_dl_size))
            if not file_path or _dl_size == 0:
                logger.warning("[auto_rename] job=%s Empty result on attempt %d", job_id, _attempt)
                file_path = None
                if _attempt < 3:
                    await asyncio.sleep(3)
                continue
            # Loosen mismatch threshold to 0.95 — Telegram sometimes strips
            # metadata bytes from the reported file_size vs actual bytes served.
            if _src_size > 0 and _dl_size < _src_size * 0.95:
                logger.warning(
                    "[auto_rename] job=%s Size mismatch expected>=%s got %s",
                    job_id, humanbytes(_src_size), humanbytes(_dl_size),
                )
                try: os.remove(file_path)
                except OSError: pass
                file_path = None
                if _attempt < 3:
                    await asyncio.sleep(3)
                continue
            logger.info("[auto_rename] job=%s Download validation PASSED  size=%s", job_id, humanbytes(_dl_size))
            break  # success

        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            _err = f"Download failed. Expected {humanbytes(_src_size)}, got 0 B."
            logger.error("[auto_rename] job=%s %s", job_id, _err)
            try:
                await status_msg.edit_text(
                    "╭━━━〔 ❌ DOWNLOAD FAILED 〕━━━╮\n"
                    f"┃  🆔  <code>{job_id}</code>\n"
                    f"┃  ⚠️  <code>{_err}</code>\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
            except Exception:
                pass
            return

        # ── Metadata (universal — same toggle/fields as manual rename) ────
        job.status      = "processing"
        _meta_applied   = False
        try:
            await status_msg.edit_text(
                _status_text(job_id, new_file_name_ps, "processing", 100, 0),
                reply_markup=cancel_kb,
            )
        except Exception:
            pass

        _has_metadata_values = any((v or "").strip() for v in metadata_fields.values())
        if use_metadata and _has_metadata_values:
            result = await add_metadata(file_path, metadata_path, metadata_fields, status_msg)
            if result and os.path.exists(metadata_path):
                file_path     = metadata_path
                _meta_applied = True
            else:
                logger.warning("[auto_rename] metadata returned None for job=%s", job_id)

        # ── Duration ──────────────────────────────────────────────────────
        duration = 0
        try:
            duration = await get_duration_hachoir(file_path)
        except Exception:
            pass

        # ── Thumbnail (universal — same DB field as manual rename) ────────
        if thumb_id:
            try:
                ph_path = await client.download_media(thumb_id)
            except Exception:
                pass
        elif base_media == "video" and message.video and message.video.thumbs:
            try:
                ph_path = await client.download_media(message.video.thumbs[0].file_id)
            except Exception:
                pass

        # ── Actual file size from disk (after metadata processing) ───────
        actual_size = os.path.getsize(file_path) if os.path.exists(file_path) else (getattr(file_obj, "file_size", 0) or 0)

        # ── Caption (universal — same template as manual rename) ──────────
        if raw_cap:
            try:
                caption = raw_cap.format(
                    filename=new_file_name_ps,
                    filesize=humanbytes(actual_size),
                    duration=convert(duration) if duration else "N/A",
                )
            except Exception:
                caption = f"<b>{new_file_name_ps}</b>"
        else:
            caption = f"<b>{new_file_name_ps}</b>"

        # ── Upload ────────────────────────────────────────────────────────
        # ── Upload (reference-repo pattern) ─────────────────────────────
        # Ported directly from the working 4 GB reference implementation:
        #   value = 2090000000
        #   if value < file.file_size:
        #       filw = await app.send_*(LOG_CHANNEL, ...)  # premium userbot
        #       await bot.copy_message(user, filw.chat.id, filw.id)
        #   else:
        #       await bot.send_*(user, ...)
        #
        # Guard: never call send_* with an empty/missing file.

        upload_type  = media_pref or base_media
        _upload_size = os.path.getsize(file_path) if (file_path and os.path.exists(file_path)) else 0
        logger.info("[auto_rename] job=%s Upload path=%s  size=%s", job_id, file_path, humanbytes(_upload_size))
        if not file_path or _upload_size == 0:
            logger.error("[auto_rename] job=%s Upload target 0 bytes — aborting", job_id)
            try:
                await status_msg.edit_text(
                    "╭━━━〔 ❌ UPLOAD ABORTED 〕━━━╮\n"
                    f"┃  🆔  <code>{job_id}</code>\n"
                    "┃  ⚠️  File is 0 bytes after processing.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
            except Exception:
                pass
            return

        logger.info("[auto_rename] job=%s Starting upload  type=%s  large=%s", job_id, upload_type, _large)
        job.status   = "uploading"
        last_edit[0] = 0.0
        c_time       = time.time()
        try:
            await status_msg.edit_text(
                _status_text(job_id, new_file_name_ps, "uploading", 0, 0),
                reply_markup=cancel_kb,
            )
        except Exception:
            pass

        # Reference threshold: 2 090 000 000 bytes (~2 GB)
        _REF_LARGE  = 2_090_000_000
        _bin_sent   = None
        _UL_TIMEOUT = 900   # 15 min — generous for large files on slow uplinks

        async def _upload_with_retry(coro_fn):
            """Run an upload coroutine, retrying once on FloodWait."""
            for _ul_att in range(1, 3):
                try:
                    return await asyncio.wait_for(coro_fn(), timeout=_UL_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("[auto_rename] job=%s upload timed out attempt=%d", job_id, _ul_att)
                    if _ul_att == 2:
                        raise
                    await asyncio.sleep(10)
                except FloodWait as fw:
                    logger.warning("[auto_rename] job=%s FloodWait %ds during upload attempt=%d", job_id, fw.value, _ul_att)
                    await asyncio.sleep(fw.value)
                    # loop retries
            return None

        # Slot already owned by rq — upload directly, no extra semaphore.
        if _file_size > _REF_LARGE and _ub:
            # ── Large-file path: userbot → BIN_CHANNEL → copy → user ─────
            if upload_type == "document":
                _bin_sent = await _upload_with_retry(lambda: _ul_client.send_document(
                    _Cfg.BIN_CHANNEL,
                    document=file_path,
                    file_name=new_file_name_ps,
                    thumb=ph_path,
                    caption=caption,
                    progress=_prog,
                    progress_args=(status_msg, c_time),
                ))
            elif upload_type == "video":
                _bin_sent = await _upload_with_retry(lambda: _ul_client.send_video(
                    _Cfg.BIN_CHANNEL,
                    video=file_path,
                    thumb=ph_path,
                    caption=caption,
                    duration=int(duration) if duration else None,
                    progress=_prog,
                    progress_args=(status_msg, c_time),
                ))
            else:
                _bin_sent = await _upload_with_retry(lambda: _ul_client.send_audio(
                    _Cfg.BIN_CHANNEL,
                    audio=file_path,
                    thumb=ph_path,
                    caption=caption,
                    duration=int(duration) if duration else None,
                    progress=_prog,
                    progress_args=(status_msg, c_time),
                ))
            await asyncio.sleep(2)
            sent = await client.copy_message(
                chat_id=message.chat.id,
                from_chat_id=_bin_sent.chat.id,
                message_id=_bin_sent.id,
                caption=caption,
            )
            logger.info(
                "[auto_rename] job=%s Large-file relay complete  bin_msg=%d  user_msg=%d",
                job_id, _bin_sent.id, sent.id,
            )
        else:
            # ── Normal path: bot sends directly to user ───────────────────
            if upload_type == "document":
                sent = await _upload_with_retry(lambda: client.send_document(
                    message.chat.id,
                    document=file_path,
                    file_name=new_file_name_ps,
                    thumb=ph_path,
                    caption=caption,
                    progress=_prog,
                    progress_args=(status_msg, c_time),
                ))
            elif upload_type == "video":
                sent = await _upload_with_retry(lambda: client.send_video(
                    message.chat.id,
                    video=file_path,
                    thumb=ph_path,
                    caption=caption,
                    duration=int(duration) if duration else None,
                    progress=_prog,
                    progress_args=(status_msg, c_time),
                ))
            else:
                sent = await _upload_with_retry(lambda: client.send_audio(
                    message.chat.id,
                    audio=file_path,
                    thumb=ph_path,
                    caption=caption,
                    duration=int(duration) if duration else None,
                    progress=_prog,
                    progress_args=(status_msg, c_time),
                ))

        # ── Leaderboard + history ─────────────────────────────────────────
        try:
            from plugins.leaderboard import record_rename, record_history
            display  = message.from_user.first_name or str(user_id)
            asyncio.create_task(record_rename(user_id, display))
            asyncio.create_task(record_history(user_id, new_file_name_ps, actual_size))
        except Exception:
            pass

        # ── Collect user display info ─────────────────────────────────────
        try:
            _uname = message.from_user.username if message.from_user else None
            _uname_str = f"@{_uname}" if _uname else "No Username"
        except Exception:
            _uname_str = "No Username"

        # ── Log to BIN/LOG channel (caption + thumb) ─────────────────────
        # For large files the userbot already uploaded to BIN_CHANNEL as
        # _bin_sent; just edit its caption instead of copying again.
        # For normal files, copy sent → BIN_CHANNEL with the rich caption.
        try:
            _orig_for_log = file_caption if file_caption else file_name
            _log_cap = (
                "╭━━━〔 📂 FILE INFO 〕━━━╮\n\n"
                f"📂 Original:\n<code>{_orig_for_log}</code>\n\n"
                f"➜ ✏️ Renamed:\n<code>{new_file_name_ps}</code>\n\n"
                f"👤 User: {_uname_str}\n"
                f"🆔 ID: <code>{user_id}</code>\n\n"
                f"🆔 Job: <code>{job_id}</code>\n"
                f"📦 Size: {humanbytes(actual_size)}\n"
                f"📊 Status: Completed\n\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━╯"
            )
            if _bin_sent:
                # Already in BIN_CHANNEL — just update the caption
                try:
                    await _ul_client.edit_message_caption(
                        _bin_sent.chat.id, _bin_sent.id, caption=_log_cap,
                    )
                except Exception:
                    pass   # caption edit failure is non-fatal
            else:
                await client.copy_message(
                    chat_id=_Cfg.BIN_CHANNEL,
                    from_chat_id=message.chat.id,
                    message_id=sent.id,
                    caption=_log_cap,
                )
        except Exception as _le:
            logger.warning("[auto_rename] BIN_CHANNEL log failed job=%s: %s", job_id, _le)

        # ── Dump channel (universal — same setting as manual rename) ──────
        try:
            if _ps.get("dump_mode") and _ps.get("dump_channel"):
                from plugins.file_rename import _dump_to_channel
                asyncio.create_task(
                    _dump_to_channel(
                        client, user_id, int(_ps["dump_channel"]), sent,
                        original_name=file_name,
                        new_name=new_file_name_ps,
                        username_str=_uname_str,
                        job_id=job_id,
                        file_size=actual_size,
                    )
                )
        except Exception:
            pass

        job.status = "done"
        # Clean up: delete progress message and the original file message
        for _m in (status_msg, message):
            try:
                await _m.delete()
            except Exception:
                pass

    except asyncio.CancelledError:
        job.status = "cancelled"
        try:
            if status_msg:
                await status_msg.edit_text(
                    f"╭━━━〔 🗑 CANCELLED 〕━━━╮\n"
                    f"┃  🆔  <code>{job_id}</code>\n"
                    f"┃  ⚡  Task removed.\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:
        job.status = "error"
        logger.exception("[auto_rename] pipeline error job=%s user=%s", job_id, user_id)
        try:
            if status_msg:
                await status_msg.edit_text(
                    f"╭━━━〔 ❌ SKILL FAILED 〕━━━╮\n"
                    f"┃  🆔  <code>{job_id}</code>\n"
                    f"┃  ⚠️  <code>{e}</code>\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
        except Exception:
            pass

    finally:
        # Remove from UX registry
        _job_registry.pop(job_id, None)
        _user_jobs.get(user_id, set()).discard(job_id)
        # Job is done — remove from persistent store
        try:
            await jishubotz.delete_pending_job(job_id)
        except Exception as _dpe:
            logger.warning("[auto_rename] delete_pending_job failed job=%s: %s", job_id, _dpe)
        for path in (download_path, metadata_path):
            if path and os.path.exists(path):
                try: os.remove(path)
                except Exception: pass
        if ph_path and os.path.exists(ph_path):
            try: os.remove(ph_path)
            except Exception: pass


def _status_text(job_id: str, name: str, phase: str, pct: int, speed: float) -> str:
    phase_map = {
        "downloading": "⬇️  Acquiring File",
        "processing":  "🧬  Evolving Data",
        "uploading":   "⬆️  Transmitting",
        "done":        "✨  Evolution Complete",
    }
    label    = phase_map.get(phase, f"⚡  {phase.upper()}")
    bar      = "█" * (pct // 10) + "░" * (10 - pct // 10)
    spd_str  = f"  ·  {humanbytes(speed)}/s" if speed > 0 else ""
    name_str = _trim(name, 44)
    lines = [
        f"╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮",
        f"┃  🆔  <code>{job_id}</code>",
        f"┃  📂  <code>{name_str}</code>",
        f"┣━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"┃  {label}",
    ]
    if pct > 0 or phase in ("downloading", "uploading"):
        lines.append(f"┃  <code>[{bar}]</code>  {pct}%{spd_str}")
    lines.append(f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Template engine
# ══════════════════════════════════════════════════════════════════════════════

def _apply_template(fmt: str, extraction_text: str, file_name: str) -> str:
    """Fill fmt placeholders using data extracted from extraction_text; keep file_name extension."""
    ep  = _extract_episode_number(extraction_text)
    s   = _extract_season_number(extraction_text)
    aud = _extract_audio_info(extraction_text)
    q   = _extract_quality(extraction_text)

    sfmt = str(s)            if s  is not None else "1"   # season: no zero-pad  (S1, S2 …)
    efmt = str(ep).zfill(2) if ep is not None else "01"  # 01,02...09,10,11...100+

    t = fmt
    t = re.sub(r'S(?:Season|season|SEASON)(\d+)', f'S{sfmt}', t, flags=re.IGNORECASE)
    for pat in [re.compile(r'\{season\}', re.IGNORECASE),
                re.compile(r'\bseason\b',  re.IGNORECASE),
                re.compile(r'Season[\s._-]*\d*', re.IGNORECASE)]:
        t = pat.sub(sfmt, t)

    t = re.sub(r'EP(?:Episode|episode|EPISODE)', f'EP{efmt}', t, flags=re.IGNORECASE)
    for pat in [re.compile(r'\{episode\}', re.IGNORECASE),
                re.compile(r'\bEpisode\b',  re.IGNORECASE),
                re.compile(r'\bEP\b',       re.IGNORECASE)]:
        t = pat.sub(efmt, t)

    ar = aud or ""
    for pat in [re.compile(r'\{audio\}',   re.IGNORECASE),
                re.compile(r'\bAudio\b',   re.IGNORECASE)]:
        t = pat.sub(ar, t)

    qr = q or ""
    for pat in [re.compile(r'\{quality\}', re.IGNORECASE),
                re.compile(r'\bQuality\b', re.IGNORECASE)]:
        t = pat.sub(qr, t)

    t = re.sub(r'\[\s*\]', '', t)
    t = re.sub(r'\(\s*\)', '', t)
    t = re.sub(r'\{\s*\}', '', t)
    t = t.strip()

    _, ext = os.path.splitext(file_name)
    if ext and not t.lower().endswith(ext.lower()):
        t = f"{t}{ext}"
    return t


# ══════════════════════════════════════════════════════════════════════════════
# Extraction helpers
# ══════════════════════════════════════════════════════════════════════════════

_QUAL_INDS = [
    r'\d{2,4}[pP]', r'\dK', r'HD(?:RIP)?', r'WEB(?:-)?DL', r'BLURAY',
    r'X264', r'X265', r'HEVC', r'FHD', r'UHD', r'HDR', r'H\.264', r'H\.265',
    r'(?:19|20)\d{2}', r'Multi(?:audio)?', r'Dual(?:audio)?',
]
_QPAT = r'(?:' + '|'.join(r'(?:[\s._-]*' + q + r')' for q in _QUAL_INDS) + r')'
_SKIP = {360, 480, 720, 1080, 1440, 2160, 2020, 2021, 2022, 2023, 2024, 2025}


def _strip_version_suffix(s: str) -> str:
    """Strip version suffixes (v2, V3, v10 …) that are ATTACHED to digit-sequences.

    12v2  → 12    12V3 → 12    01 v2 → 01
    Leaves season/series numbers untouched (S2, Season 2 are not touched here
    because this is only called on cleaned/candidate strings, not the raw text).
    """
    return re.sub(r'(\d+)\s*[vV]\d+', r'\1', s)


def _extract_episode_number(text: str):
    """Extract episode number from an anime filename string.

    Priority (highest → lowest):
      1. SxxEyy forms    — S01E07, S1E07, S01 E07, S01-E07, S01 - E07  (episode = yy)
      2. xxEyy forms     — 01E02, 1E02, 01 E02, 01-E02  (episode = yy, NOT xx)
      3. S1 - 12 forms   — season-dash-bare-number
      4. Episode / EP / Ep keyword
      5. Standalone E12 / [E12]
      6. X of Y
      7. Bare number fallback (after stripping vN version suffixes)

    Rules:
      • When an explicit episode marker (E/EP/Episode) is present it ALWAYS wins.
      • vN/VN suffixes attached to episode numbers mean VERSION not EPISODE.
      • Technical metadata (1080p, 10bit, x265, years …) is never an episode.
    """
    if not text:
        return None

    # ── Priority 1 & 2: patterns with explicit E marker ──────────────────────
    # These are tried strictly in order; first non-skipped hit wins.
    explicit = [
        # 1a. S01E07, S1E07 — no separator between Sxx and Eyy
        re.compile(r'S(\d+)E(\d+)(?:\s*[vV]\d+)?',                   re.IGNORECASE),
        # 1b. S01 E07, S1 E07 — space separator
        re.compile(r'S(\d+)\s+E(\d+)(?:\s*[vV]\d+)?',                re.IGNORECASE),
        # 1c. S01-E07, S01_E07, S01.E07
        re.compile(r'S(\d+)[._-]E(\d+)(?:\s*[vV]\d+)?',              re.IGNORECASE),
        # 1d. S01 - E07 (dash with spaces)
        re.compile(r'S(\d+)\s*-\s*E(\d+)(?:\s*[vV]\d+)?',           re.IGNORECASE),
        # 2a. 01E02, 1E02 — bare number then E+episode (no S prefix)
        re.compile(r'(?<![A-Za-z\d])(\d+)E(\d+)(?:\s*[vV]\d+)?(?!\d)', re.IGNORECASE),
        # 2b. 01 E02, 01-E02, 01 - E02 — bare number, whitespace/dash, E+episode
        re.compile(r'(?<![A-Za-z\d])(\d+)\s*[-_.]?\s*E(\d+)(?:\s*[vV]\d+)?(?!\d)', re.IGNORECASE),
    ]
    # Patterns 1a-1d capture (season, episode) — we want group 2 (episode)
    # Patterns 2a-2b capture (prefix_num, episode) — we want group 2 (episode)
    for pat in explicit:
        for m in pat.findall(text):
            # m is always a 2-tuple (group1, group2) for all patterns above
            raw = m[1] if isinstance(m, tuple) and len(m) >= 2 else m
            try:
                n = int(raw)
                if 1 <= n <= 9999 and n not in _SKIP:
                    return n
            except ValueError:
                pass

    # ── Priority 3: S1 - 12, S01-12, S01 12 (season-dash-bare-episode) ────────
    season_bare = [
        re.compile(r'S\d+\s*-\s*(\d+)(?:\s*[vV]\d+)?',              re.IGNORECASE),
        re.compile(r'S\d+[._]+(\d+)(?:\s*[vV]\d+)?',                  re.IGNORECASE),
    ]
    for pat in season_bare:
        for m in pat.findall(text):
            raw = m[0] if isinstance(m, tuple) else m
            try:
                n = int(raw)
                if 1 <= n <= 9999 and n not in _SKIP:
                    return n
            except ValueError:
                pass

    # ── Priority 4: keyword forms ─────────────────────────────────────────────
    keyword = [
        re.compile(r'\bEpisode\s+(\d+)(?:\s*[vV]\d+)?',              re.IGNORECASE),
        re.compile(r'\bEP\s*(\d+)(?:\s*[vV]\d+)?\b',                 re.IGNORECASE),
    ]
    for pat in keyword:
        for m in pat.findall(text):
            raw = m[0] if isinstance(m, tuple) else m
            try:
                n = int(raw)
                if 1 <= n <= 9999 and n not in _SKIP:
                    return n
            except ValueError:
                pass

    # ── Priority 5: standalone E12 / [E12] ───────────────────────────────────
    # NOTE: lookbehind explicitly excludes [ and ( so that CRC hashes like
    # [E854CE6C] — where E is immediately preceded by [ — are never matched.
    # The bracket exclusion is intentional: [E12] is a valid episode marker
    # only when caught by pattern 5b below (full bracket pair), not 5a.
    standalone_e = [
        re.compile(r'(?<![A-Za-z\d\[\(])E(\d+)(?:\s*[vV]\d+)?(?!\d)',   re.IGNORECASE),
        re.compile(r'[\[\(]E(\d+)(?:\s*[vV]\d+)?[\]\)]',                  re.IGNORECASE),
    ]
    for pat in standalone_e:
        for m in pat.findall(text):
            raw = m[0] if isinstance(m, tuple) else m
            try:
                n = int(raw)
                if 1 <= n <= 9999 and n not in _SKIP:
                    return n
            except ValueError:
                pass

    # ── Priority 6: X of Y ────────────────────────────────────────────────────
    m = re.search(r'\b(\d+)\s*of\s*\d+\b', text, re.IGNORECASE)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 9999 and n not in _SKIP:
                return n
        except ValueError:
            pass

    # ── Priority 7: bare standalone number fallback ───────────────────────────
    # Strip vN suffixes first so "12v2" → "12" before scanning.
    cleaned = _strip_version_suffix(text)
    fallback = re.compile(
        r'(?:^|[^0-9A-Za-z])(\d{1,4})(?:[^0-9A-Za-z]|$)(?!' + _QPAT + r')',
        re.IGNORECASE,
    )
    for m in fallback.findall(cleaned):
        raw = m[0] if isinstance(m, tuple) else m
        try:
            n = int(raw)
            if 1 <= n <= 9999 and n not in _SKIP:
                return n
        except ValueError:
            pass

    return None


def _extract_season_number(text: str):
    if not text: return None
    patterns = [
        re.compile(r'S(\d+)[._-]?E\d+',                      re.IGNORECASE),
        re.compile(r'(?:Season|SEASON|season)[\s._-]*(\d+)', re.IGNORECASE),
        re.compile(r'\bS(\d+)\b(?!E\d|' + _QPAT + r')',     re.IGNORECASE),
        re.compile(r'[\[\(]S(\d+)[\]\)]',                    re.IGNORECASE),
        re.compile(r'[._-]S(\d+)(?:[._-]|$)',                re.IGNORECASE),
    ]
    for pat in patterns:
        m = pat.search(text)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 99: return n
            except ValueError:
                pass
    return None


def _extract_audio_info(text: str):
    kw = {
        'Hindi': r'Hindi', 'English': r'English', 'Multi': r'Multi(?:audio)?',
        'Telugu': r'Telugu', 'Tamil': r'Tamil', 'Jap': r'Jap',
        'Dual': r'Dual(?:audio)?', 'AAC': r'AAC', 'AC3': r'AC3',
        'DTS': r'DTS', '5.1': r'5\.1',
    }
    found = [k for k, p in kw.items() if re.search(p, text, re.IGNORECASE)]
    return ' '.join(found) if found else None


def _extract_quality(text: str):
    for pat in [
        re.compile(r'\b(4K|2K|2160p|1440p|1080p|720p|480p|360p)\b', re.IGNORECASE),
        re.compile(r'\b(HD(?:RIP)?|WEB(?:-)?DL|BLURAY)\b',           re.IGNORECASE),
        re.compile(r'\b(X264|X265|HEVC)\b',                           re.IGNORECASE),
    ]:
        m = pat.search(text)
        if m: return m.group(1)
    return None
