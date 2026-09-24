"""
plugins/leaderboard.py
───────────────────────
/leaderboard — Rename stats with Today / Weekly / Monthly / All-Time filters.
/history      — Last 20 renamed files.

Public API:
    record_rename(user_id, display_name)
    record_history(user_id, filename, filesize)
"""

from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from helper.database import jishubotz
from helper.utils import humanbytes

_PAGE = 10

# ── Date keys ──────────────────────────────────────────────────────────────────

def _keys() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "day":   now.strftime("%Y-%m-%d"),
        "week":  now.strftime("%Y-W%W"),
        "month": now.strftime("%Y-%m"),
    }


# ── Public API ─────────────────────────────────────────────────────────────────

async def record_rename(user_id: int, display_name: str) -> None:
    k   = _keys()
    col = jishubotz.jishubotz.rename_stats
    await col.update_one(
        {"_id": int(user_id)},
        {
            "$set": {"name": display_name},
            "$inc": {
                f"daily.{k['day']}":     1,
                f"weekly.{k['week']}":   1,
                f"monthly.{k['month']}": 1,
                "total":                 1,
            },
        },
        upsert=True,
    )


async def record_history(user_id: int, filename: str, filesize: int) -> None:
    col   = jishubotz.jishubotz.rename_history
    entry = {
        "name": filename,
        "size": filesize,
        "ts":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    await col.update_one(
        {"_id": int(user_id)},
        {"$push": {"entries": {"$each": [entry], "$position": 0, "$slice": 20}}},
        upsert=True,
    )


# ── DB fetch ───────────────────────────────────────────────────────────────────

async def _fetch(period: str, page: int = 0) -> tuple[list[dict], int]:
    col = jishubotz.jishubotz.rename_stats
    k   = _keys()

    field = {
        "today":   f"daily.{k['day']}",
        "weekly":  f"weekly.{k['week']}",
        "monthly": f"monthly.{k['month']}",
    }.get(period, "total")

    pipeline = [
        {"$project": {"name": 1, "count": {"$ifNull": [f"${field}", 0]}}},
        {"$match":   {"count": {"$gt": 0}}},
        {"$sort":    {"count": -1}},
    ]
    all_docs = await jishubotz.jishubotz.rename_stats.aggregate(pipeline).to_list(None)
    return all_docs[page * _PAGE:(page + 1) * _PAGE], len(all_docs)


# ── Leaderboard UI ─────────────────────────────────────────────────────────────

_PERIODS = {
    "today":   "Today",
    "weekly":  "Weekly",
    "monthly": "Monthly",
    "alltime": "All Time",
}

_HEADERS = {
    "today":   "Today",
    "weekly":  "This Week",
    "monthly": "This Month",
    "alltime": "All Time",
}


def _lb_text(entries: list, period: str, page: int, total: int) -> str:
    period_label = _HEADERS.get(period, period.title())
    header = f"╭━━━〔 🏆 TEMPEST RANKINGS 〕━━━╮\n┃  ⚡  Period  ·  {period_label}"
    if not entries:
        return f"{header}\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n<i>No renames recorded yet.</i>"

    lines  = [header, "┣━━━━━━━━━━━━━━━━━━━━━━━━━"]
    offset = page * _PAGE
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    for i, e in enumerate(entries):
        rank   = offset + i + 1
        prefix = medals.get(rank, f"<code>{rank:>2}</code>  ")
        name   = (e.get("name") or "Unknown")[:22]
        count  = e["count"]
        lines.append(f"┃  {prefix}  {name}  ·  <b>{count:,}</b>")

    pages = max(1, (total + _PAGE - 1) // _PAGE)
    lines += [
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
        f"<i>Page  ·  {page+1} / {pages}</i>"
    ]
    return "\n".join(lines)


def _lb_markup(period: str, page: int, total: int) -> InlineKeyboardMarkup:
    pages = max(1, (total + _PAGE - 1) // _PAGE)
    rows  = []
    row   = []
    for i, (p, label) in enumerate(_PERIODS.items()):
        marker = "· " if p == period else ""
        row.append(InlineKeyboardButton(f"{marker}{label}", callback_data=f"lb_{p}_0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"lb_{period}_{page-1}"))
        nav.append(InlineKeyboardButton(f"📑 {page+1}/{pages}", callback_data="lb_noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"lb_{period}_{page+1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton("✕ Dismiss", callback_data="lb_close")])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.private & filters.command("leaderboard"))
async def cmd_leaderboard(client: Client, message: Message):
    entries, total = await _fetch("today")
    text   = _lb_text(entries, "today", 0, total)
    markup = _lb_markup("today", 0, total)
    await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^lb_"))
async def cb_leaderboard(client: Client, query: CallbackQuery):
    data = query.data

    if data == "lb_noop":
        return await query.answer()

    if data == "lb_close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    parts = data.split("_")
    if len(parts) != 3:
        return await query.answer()

    period, page_str = parts[1], parts[2]
    try:
        page = int(page_str)
    except ValueError:
        page = 0

    if period not in _PERIODS:
        return await query.answer()

    await query.answer()
    entries, total = await _fetch(period, page)
    text   = _lb_text(entries, period, page, total)
    markup = _lb_markup(period, page, total)

    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


# ── /history ───────────────────────────────────────────────────────────────────

async def _fetch_history(user_id: int) -> list:
    col = jishubotz.jishubotz.rename_history
    doc = await col.find_one({"_id": int(user_id)})
    return doc.get("entries", []) if doc else []


def _history_text(entries: list) -> str:
    if not entries:
        return (
            "╭━━━〔 📂 EVOLUTION LOG 〕━━━╮\n"
            "┃  ⚠️  No history yet.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "<i>Send a file to get started.</i>"
        )
    lines = ["╭━━━〔 📂 EVOLUTION LOG 〕━━━╮"]
    for i, e in enumerate(entries, 1):
        name  = e.get("name", "Unknown")
        size  = humanbytes(e.get("size", 0))
        ts    = e.get("ts", "")[:16].replace("T", "  ")
        lines.append(f"<b>{i}.</b>  <code>{name}</code>")
        lines.append(f"      📦 {size}  ·  🕒 <i>{ts}</i>\n")
    return "\n".join(lines).rstrip()


@Client.on_message(filters.private & filters.command(["history", "h"]))
async def cmd_history(client: Client, message: Message):
    entries = await _fetch_history(message.from_user.id)
    await message.reply_text(
        _history_text(entries),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Clear", callback_data="hist_clear"),
            InlineKeyboardButton("✕ Dismiss", callback_data="hist_close"),
        ]]),
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex(r"^hist_(clear|close)$"))
async def cb_history(client: Client, query: CallbackQuery):
    action = query.data.split("_")[1]

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    await query.answer("History cleared.")
    await jishubotz.jishubotz.rename_history.delete_one({"_id": int(query.from_user.id)})
    try:
        await query.message.edit_text(
            "╭━━━〔 📂 EVOLUTION LOG 〕━━━╮\n┃  ✨  History cleared.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✕ Dismiss", callback_data="hist_close")
            ]]),
        )
    except Exception:
        pass
