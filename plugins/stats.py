"""
plugins/stats.py
────────────────
/stats  —  Admin-only real system statistics panel.

Shows CPU, RAM, disk, download/upload speed, bandwidth, active/queued jobs,
transmission usage, uptime, and DB status.
"""

from __future__ import annotations

import asyncio
import os
import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from config import Config
from helper.utils import humanbytes

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def _build_stats_text() -> str:
    from helper.queue_manager import qm as _qm
    from helper.database    import jishubotz

    manager = _qm()

    # ── System stats ─────────────────────────────────────────────────────
    if _HAS_PSUTIL:
        cpu_pct  = psutil.cpu_percent(interval=0.2)
        ram      = psutil.virtual_memory()
        ram_used = ram.used
        ram_tot  = ram.total
        ram_pct  = ram.percent
        disk     = psutil.disk_usage("/")
        disk_use = disk.used
        disk_tot = disk.total
        disk_pct = disk.percent
        disk_free = disk.free
        cpu_str  = f"{cpu_pct:.1f}%"
        ram_str  = f"{humanbytes(ram_used)} / {humanbytes(ram_tot)} ({ram_pct:.0f}%)"
        disk_str = f"{humanbytes(disk_use)} / {humanbytes(disk_tot)} ({disk_pct:.0f}%)"
        free_str = humanbytes(disk_free)
    else:
        cpu_str  = "N/A (install psutil)"
        ram_str  = "N/A"
        disk_str = "N/A"
        free_str = "N/A"

    # ── Bandwidth ─────────────────────────────────────────────────────────
    dl_spd = manager.dl_speed()
    ul_spd = manager.ul_speed()
    bw_tot = dl_spd + ul_spd
    dl_str = humanbytes(int(dl_spd)) + "/s" if dl_spd > 0 else "0 B/s"
    ul_str = humanbytes(int(ul_spd)) + "/s" if ul_spd > 0 else "0 B/s"
    bw_str = humanbytes(int(bw_tot)) + "/s" if bw_tot > 0 else "0 B/s"

    # ── Jobs ─────────────────────────────────────────────────────────────
    active  = manager.active_count()
    avail   = manager.available_slots()
    t_lim   = manager.transmission_limit   # == rq.concurrency via shim
    t_used  = active                       # jobs currently processing

    queued = manager.queued_count()   # via shim → rq.queued_count()

    # ── DB status ─────────────────────────────────────────────────────────
    try:
        await jishubotz.db.command("ping")
        db_status = "✅"
    except Exception:
        db_status = "❌"

    uptime_str = _fmt_uptime(manager.uptime_seconds())

    return (
        "╭━━━〔 📊 SYSTEM STATS 〕━━━╮\n"
        "┃\n"
        f"┃  🖥️  CPU Usage       ·  {cpu_str}\n"
        f"┃  🧠  RAM Usage       ·  {ram_str}\n"
        f"┃  💾  Disk Usage      ·  {disk_str}\n"
        f"┃  📦  Free Disk       ·  {free_str}\n"
        "┃\n"
        f"┃  📥  Download        ·  {dl_str}\n"
        f"┃  📤  Upload          ·  {ul_str}\n"
        f"┃  🌐  Total Bandwidth ·  {bw_str}\n"
        "┃\n"
        f"┃  ⚡  Active Jobs     ·  {active}\n"
        f"┃  📋  Queued Jobs     ·  {queued}\n"
        f"┃  📡  Transmission    ·  {t_used} / {t_lim}\n"
        "┃\n"
        f"┃  🔄  Uptime          ·  {uptime_str}\n"
        f"┃  💾  DB Sync         ·  {db_status}\n"
        "┃\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


@Client.on_message(filters.command("stats") & filters.user(Config.ADMIN))
async def cmd_stats(client: Client, message: Message):
    msg = await message.reply_text("⏳ Gathering system stats…")
    try:
        text = await _build_stats_text()
        await msg.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="stats_refresh"),
            ]]),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


@Client.on_callback_query(filters.regex(r"^stats_refresh$"))
async def cb_stats_refresh(client: Client, update: CallbackQuery):
    if update.from_user.id not in Config.ADMIN:
        return await update.answer("⛔ Admin only.", show_alert=True)
    try:
        text = await _build_stats_text()
        await update.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="stats_refresh"),
            ]]),
        )
        await update.answer("✅ Refreshed")
    except Exception as e:
        await update.answer(f"Error: {e}", show_alert=True)
