"""
plugins/task_status.py
───────────────────────
/status (/s) — Live task monitor.

Public API (imported by file_rename.py):
    register_task, set_task_ref, update_task_progress, finish_task, is_cancelled
"""

import asyncio
import time
from dataclasses import dataclass, field

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from helper.utils import humanbytes, TimeFormatter

_PER_PAGE = 3

# ── Task store ─────────────────────────────────────────────────────────────────

@dataclass
class TaskInfo:
    job_id:    str
    user_id:   int
    user_name: str
    filename:  str
    total:     int   = 0
    done:      int   = 0
    speed:     float = 0.0
    eta_s:     float = 0.0
    elapsed_s: float = 0.0
    status:    str   = "Starting"
    start_ts:  float = field(default_factory=time.time)
    cancelled: bool  = False
    _task_ref: object = field(default=None, repr=False)


_tasks: dict[str, TaskInfo] = {}
_lock  = asyncio.Lock()


# ── Public API ─────────────────────────────────────────────────────────────────

async def register_task(job_id: str, user_id: int, user_name: str, filename: str) -> None:
    async with _lock:
        _tasks[job_id] = TaskInfo(
            job_id=job_id, user_id=user_id,
            user_name=user_name, filename=filename,
        )


def set_task_ref(job_id: str, task: asyncio.Task) -> None:
    t = _tasks.get(job_id)
    if t:
        t._task_ref = task


async def update_task_progress(
    job_id: str, done: int, total: int,
    speed: float, eta_s: float, status: str,
) -> None:
    async with _lock:
        t = _tasks.get(job_id)
        if t:
            t.done      = done
            t.total     = total
            t.speed     = speed
            t.eta_s     = eta_s
            t.status    = status
            t.elapsed_s = time.time() - t.start_ts


async def finish_task(job_id: str) -> None:
    async with _lock:
        _tasks.pop(job_id, None)


def is_cancelled(job_id: str) -> bool:
    t = _tasks.get(job_id)
    return t.cancelled if t else False


# ── Rendering ──────────────────────────────────────────────────────────────────

def _render(t: TaskInfo) -> str:
    pct   = (t.done / t.total * 100) if t.total > 0 else 0
    bar   = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
    eta   = TimeFormatter(int(t.eta_s * 1000)) or "…"
    ela   = TimeFormatter(int(t.elapsed_s * 1000)) or "0s"
    spd   = f"{humanbytes(t.speed)}/s" if t.speed > 0 else "—"
    done  = humanbytes(t.done) if t.done else "—"
    total = humanbytes(t.total) if t.total else "—"
    phase_icon = {"Downloading": "⬇️", "Uploading": "⬆️", "Processing": "🧬"}.get(t.status, "⚡")
    return (
        f"╭━━━〔 💠 {t.filename[:30]} 〕\n"
        f"┃  🆔  <code>{t.job_id}</code>\n"
        f"┃  {phase_icon}  {t.status}  ·  {pct:.1f}%\n"
        f"┃  <code>[{bar}]</code>\n"
        f"┃  📦  {done} / {total}\n"
        f"┃  ⚡  {spd}  ·  ⏱ {eta}\n"
        f"┃  🕐  Elapsed  ·  {ela}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


def _user_tasks(user_id: int) -> list[TaskInfo]:
    return [t for t in _tasks.values() if t.user_id == user_id]


def _text(tasks: list[TaskInfo], page: int) -> str:
    if not tasks:
        return "╭━━━〔 📋 ACTIVE TASKS 〕━━━╮\n┃  ✨  No active tasks.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    start   = page * _PER_PAGE
    visible = tasks[start : start + _PER_PAGE]
    header  = f"╭━━━〔 📋 ACTIVE TASKS · {len(tasks)} 〕━━━╮\n"
    return header + "\n\n".join(_render(t) for t in visible)


def _markup(tasks: list[TaskInfo], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(tasks) + _PER_PAGE - 1) // _PER_PAGE)
    rows = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"st_page_{page-1}"))
        nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="st_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"st_page_{page+1}"))
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="st_refresh_0"),
        InlineKeyboardButton("✕ Dismiss",  callback_data="st_close"),
    ])
    return InlineKeyboardMarkup(rows)


# ── /status ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("status"))
async def cmd_status(client: Client, message: Message):
    tasks  = _user_tasks(message.from_user.id)
    await message.reply_text(_text(tasks, 0), reply_markup=_markup(tasks, 0))


# ── /cancel_{job_id} ──────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.regex(r"^/cancel_(.+)$"))
async def cmd_cancel_task(client: Client, message: Message):
    job_id = message.matches[0].group(1)
    async with _lock:
        t = _tasks.get(job_id)
        if not t:
            return await message.reply_text("❌ Task not found — may have already finished.")
        if t.user_id != message.from_user.id:
            return await message.reply_text("❌ That's not your task.")
        t.cancelled = True
        t.status    = "Cancelling…"
        ref         = t._task_ref

    if ref and not ref.done():
        ref.cancel()

    await message.reply_text(
        f"╭━━━〔 🗑 CANCEL REQUESTED 〕━━━╮\n"
        f"┃  📂  <code>{t.filename[:38]}</code>\n"
        f"┃  ⚡  Stopping shortly…\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


# ── Callbacks ──────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^st_"))
async def cb_status(client: Client, query: CallbackQuery):
    data = query.data

    if data == "st_noop":
        await query.answer()
        return

    if data == "st_close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    try:
        page = int(data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0

    await query.answer("Refreshing…" if "refresh" in data else "")
    tasks = _user_tasks(query.from_user.id)
    try:
        await query.message.edit_text(_text(tasks, page), reply_markup=_markup(tasks, page))
    except Exception:
        pass
