"""
plugins/panel.py
─────────────────
/panel — Master admin panel.

Two tabs:
  🖼 Images   — set/preview/remove per-screen images
  ⚙️ Settings — Server URL, Link Expiry, Global/User limits, quick jumps
"""

from __future__ import annotations
import logging
import time

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from config import Config
from helper.database import jishubotz

logger = logging.getLogger(__name__)

SCREENS = {
    "start_pic":    "🏠 Start",
    "help_pic":     "❓ Help",
    "about_pic":    "ℹ️ About",
    "rename_pic":   "✏️ File Picker",
    "metadata_pic": "🏷️ Metadata",
    "dump_pic":     "📤 Dump",
}

_state: dict[int, str] = {}


# ══ Text builders ═════════════════════════════════════════════════════════════

async def _images_text() -> str:
    images = await jishubotz.get_panel_images()
    lines  = ["◈ <b>Panel  ·  Image Manager</b>\n"]
    for key, label in SCREENS.items():
        tick = "✅" if images.get(key) else "❌"
        lines.append(f"┣  {tick}  {label}")
    lines.append("\n<i>Tap a label to preview  ·  📷 Set  ·  🗑 Remove</i>")
    return "\n".join(lines)


async def _settings_text() -> str:
    from helper.queue_manager import rq as _rq
    expiry  = await jishubotz.get_expiry_minutes()
    exp_str = f"{expiry} min" if expiry > 0 else "Never ∞"
    srv_url = "<i>Disabled</i>"
    c       = _rq.concurrency
    return (
        "◈ <b>Panel  ·  Bot Settings</b>\n\n"
        "┌  <b>File-to-Link</b>\n"
        f"├  🌐 Server URL   →  <code>{srv_url}</code>\n"
        f"└  ⏱️ Link Expiry  →  <code>{exp_str}</code>\n\n"
        "┌  <b>Rename Queue</b>\n"
        f"└  🔢 Concurrency  →  <code>{c}</code> simultaneous jobs\n\n"
        "<i>Tap a button below to change a value.</i>"
    )


# ══ Markup builders ═══════════════════════════════════════════════════════════

async def _images_markup() -> InlineKeyboardMarkup:
    images = await jishubotz.get_panel_images()
    rows   = []
    for key, label in SCREENS.items():
        tick = "✅" if images.get(key) else "➕"
        rows.append([
            InlineKeyboardButton(f"{tick} {label}",  callback_data=f"pnl_img_preview_{key}"),
            InlineKeyboardButton("📷 Set",            callback_data=f"pnl_img_set_{key}"),
            InlineKeyboardButton("🗑 Remove",         callback_data=f"pnl_img_del_{key}"),
        ])
    rows += [
        [InlineKeyboardButton("⚙️ Settings →", callback_data="pnl_tab_settings")],
        [InlineKeyboardButton("✖️ Close",       callback_data="pnl_close")],
    ]
    return InlineKeyboardMarkup(rows)


def _settings_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Set Server URL",  callback_data="pnl_set_serverurl"),
            InlineKeyboardButton("🗑 Clear URL",        callback_data="pnl_clear_serverurl"),
        ],
        [
            InlineKeyboardButton("⏱ Never",  callback_data="pnl_expiry_0"),
            InlineKeyboardButton("⏱ 1 hr",   callback_data="pnl_expiry_60"),
            InlineKeyboardButton("⏱ 12 hr",  callback_data="pnl_expiry_720"),
            InlineKeyboardButton("⏱ 24 hr",  callback_data="pnl_expiry_1440"),
            InlineKeyboardButton("⏱ 7 days", callback_data="pnl_expiry_10080"),
        ],
        [InlineKeyboardButton("⏱ Custom Expiry (type minutes)", callback_data="pnl_expiry_custom")],
        [
            InlineKeyboardButton("🌍 G: 5",  callback_data="pnl_glimit_5"),
            InlineKeyboardButton("🌍 G: 10", callback_data="pnl_glimit_10"),
            InlineKeyboardButton("🌍 G: 20", callback_data="pnl_glimit_20"),
            InlineKeyboardButton("🌍 G: 50", callback_data="pnl_glimit_50"),
        ],
        [
            InlineKeyboardButton("👤 U: 1", callback_data="pnl_ulimit_1"),
            InlineKeyboardButton("👤 U: 2", callback_data="pnl_ulimit_2"),
            InlineKeyboardButton("👤 U: 3", callback_data="pnl_ulimit_3"),
            InlineKeyboardButton("👤 U: 5", callback_data="pnl_ulimit_5"),
        ],
        [
            InlineKeyboardButton("🎛 Media Settings", callback_data="pnl_open_msettings"),
            InlineKeyboardButton("📊 Stats",           callback_data="pnl_open_stats"),
        ],
        [InlineKeyboardButton("🖼 Images →", callback_data="pnl_tab_images")],
        [InlineKeyboardButton("✖️ Close",    callback_data="pnl_close")],
    ])


# ══ /panel command ════════════════════════════════════════════════════════════

@Client.on_message(
    filters.private & filters.command("panel") & filters.user(Config.ADMIN)
)
async def cmd_panel(client: Client, message: Message):
    await message.reply_text(
        await _images_text(),
        reply_markup=await _images_markup(),
    )


# ══ Callback router ═══════════════════════════════════════════════════════════

@Client.on_callback_query(
    filters.regex(r"^pnl_") & filters.user(Config.ADMIN)
)
async def cb_panel(client: Client, query: CallbackQuery):
    data    = query.data
    user_id = query.from_user.id

    # ── Close ─────────────────────────────────────────────────────────────────
    if data == "pnl_close":
        _state.pop(user_id, None)
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # ── Tab: Images ───────────────────────────────────────────────────────────
    if data == "pnl_tab_images":
        await query.answer()
        try:
            await query.message.edit_text(
                await _images_text(),
                reply_markup=await _images_markup(),
            )
        except Exception:
            pass
        return

    # ── Tab: Settings ─────────────────────────────────────────────────────────
    if data == "pnl_tab_settings":
        await query.answer()
        try:
            await query.message.edit_text(
                await _settings_text(),
                reply_markup=_settings_markup(),
            )
        except Exception:
            pass
        return

    # ══ Images tab ════════════════════════════════════════════════════════════

    if data.startswith("pnl_img_preview_"):
        key   = data[len("pnl_img_preview_"):]
        label = SCREENS.get(key, key)
        img   = (await jishubotz.get_panel_images()).get(key)
        if not img:
            await query.answer(f"No image set for {label}.", show_alert=True)
            return
        await query.answer()
        try:
            await client.send_photo(
                query.message.chat.id,
                photo=img,
                caption=f"◈ <b>Preview  ·  {label}</b>",
            )
        except Exception as e:
            await query.answer(f"Preview failed: {e}", show_alert=True)
        return

    if data.startswith("pnl_img_set_"):
        key   = data[len("pnl_img_set_"):]
        label = SCREENS.get(key, key)
        _state[user_id] = f"waiting_img_{key}"
        await query.answer()
        await query.message.reply_text(
            f"◈ <b>Set Image  ·  {label}</b>\n\n"
            f"📷 Send a photo to set for this screen.\n"
            f"Send /cancel to abort."
        )
        return

    if data.startswith("pnl_img_del_"):
        key   = data[len("pnl_img_del_"):]
        label = SCREENS.get(key, key)
        await jishubotz.del_panel_image(key)
        await query.answer(f"🗑 {label} image removed.", show_alert=True)
        try:
            await query.message.edit_text(
                await _images_text(),
                reply_markup=await _images_markup(),
            )
        except Exception:
            pass
        return

    # ══ Settings tab ══════════════════════════════════════════════════════════

    # ── Server URL ────────────────────────────────────────────────────────────
    if data == "pnl_set_serverurl":
        _state[user_id] = "waiting_serverurl"
        await query.answer()
        await query.message.reply_text(
            "◈ <b>Set Server URL</b>\n\n"
            "Send your deployment URL (no trailing slash).\n\n"
            "<b>Example:</b>\n"
            "<blockquote><code>https://my-bot.herokuapp.com</code></blockquote>\n\n"
            "Send /cancel to abort."
        )
        return

    if data == "pnl_clear_serverurl":
        await jishubotz.set_bot_setting("server_url", "")
        await query.answer("🗑 Server URL cleared.", show_alert=True)
        await _refresh_settings(query)
        return

    # ── Link expiry ───────────────────────────────────────────────────────────
    if data.startswith("pnl_expiry_"):
        val = data[len("pnl_expiry_"):]
        if val == "custom":
            _state[user_id] = "waiting_expiry"
            await query.answer()
            await query.message.reply_text(
                "◈ <b>Custom Link Expiry</b>\n\n"
                "Send the duration in <b>minutes</b>.\n\n"
                "<blockquote>"
                "0      →  Never expire\n"
                "60     →  1 hour\n"
                "1440   →  24 hours\n"
                "10080  →  7 days"
                "</blockquote>\n\n"
                "Send /cancel to abort."
            )
            return
        minutes = int(val)
        await jishubotz.set_expiry_minutes(minutes)
        Config.LINK_EXPIRY = minutes
        exp_str = f"{minutes} min" if minutes > 0 else "Never ∞"
        await query.answer(f"⏱ Expiry → {exp_str}")
        await _refresh_settings(query)
        return

    # ── Concurrency limit (replaces old global/user split) ────────────────────
    if data.startswith("pnl_glimit_"):
        from helper.queue_manager import rq as _rq
        from helper.database import jishubotz as _db
        n = int(data[len("pnl_glimit_"):])
        await _rq.set_concurrency(n)
        await _db.set_rename_concurrency(n)
        await query.answer(f"🔢 Concurrency → {n}")
        await _refresh_settings(query)
        return

    # ── User limit (legacy panel button — redirect to concurrency) ─────────────
    if data.startswith("pnl_ulimit_"):
        from helper.queue_manager import rq as _rq
        from helper.database import jishubotz as _db
        n = int(data[len("pnl_ulimit_"):])
        await _rq.set_concurrency(n)
        await _db.set_rename_concurrency(n)
        await query.answer(f"🔢 Concurrency → {n}")
        await _refresh_settings(query)
        return

    # ── Quick jumps ───────────────────────────────────────────────────────────
    if data == "pnl_open_msettings":
        await query.answer()
        from plugins.media_settings import _settings_text as ms_txt, _settings_markup as ms_mkp
        await query.message.reply_text(
            await ms_txt(query.from_user.id),
            reply_markup=ms_mkp(),
        )
        return

    if data == "pnl_open_stats":
        await query.answer()
        total = await jishubotz.total_users_count()
        uptime_secs = time.time() - getattr(client, "uptime", time.time())
        uptime_str  = time.strftime("%Hh %Mm %Ss", time.gmtime(uptime_secs))
        await query.message.reply_text(
            f"◈ <b>Bot Statistics</b>\n\n"
            f"<blockquote>"
            f"Uptime  →  {uptime_str}\n"
            f"Users   →  {total:,}"
            f"</blockquote>"
        )
        return

    await query.answer()


# ══ Input capture ════════════════════════════════════════════════════════════

@Client.on_message(
    filters.private & filters.photo & filters.user(Config.ADMIN),
    group=2,
)
async def handle_panel_photo(client: Client, message: Message):
    user_id = message.from_user.id
    state   = _state.get(user_id, "")
    if not state.startswith("waiting_img_"):
        return
    key   = state[len("waiting_img_"):]
    label = SCREENS.get(key, key)
    if key not in SCREENS:
        return
    await jishubotz.set_panel_image(key, message.photo.file_id)
    _state.pop(user_id, None)
    await message.reply_text(
        f"◈ <b>Image Saved  ·  {label}</b>\n\n"
        f"✅ Live immediately.\n"
        f"Use /panel to manage more."
    )


@Client.on_message(
    filters.private & filters.text & filters.user(Config.ADMIN),
    group=2,
)
async def handle_panel_text_input(client: Client, message: Message):
    user_id = message.from_user.id
    state   = _state.get(user_id, "")
    if not state:
        return

    text = message.text.strip()

    if text.lower() in ("/cancel", "cancel"):
        _state.pop(user_id, None)
        await message.reply_text("◈ <b>Cancelled</b>\n\nNo changes made.")
        return

    # ── Server URL ────────────────────────────────────────────────────────────
    if state == "waiting_serverurl":
        if not text.startswith("http"):
            return await message.reply_text(
                "❌ URL must start with <code>http</code>.\n"
                "Try again or send /cancel."
            )
        url = text.rstrip("/")
        await jishubotz.set_bot_setting("server_url", url)
        _state.pop(user_id, None)
        await message.reply_text(
            f"◈ <b>Server URL Saved</b>\n\n"
            f"<blockquote>URL  →  <code>{url}</code></blockquote>\n\n"
            f"✅ All new links will use this base URL."
        )
        return

    # ── Custom expiry ─────────────────────────────────────────────────────────
    if state == "waiting_expiry":
        if not text.isdigit():
            return await message.reply_text(
                "❌ Send a number in minutes.\n"
                "Example: <code>120</code> for 2 hours."
            )
        minutes = int(text)
        await jishubotz.set_expiry_minutes(minutes)
        Config.LINK_EXPIRY = minutes
        _state.pop(user_id, None)
        exp_str = f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes > 0 else "Never ∞"
        await message.reply_text(
            f"◈ <b>Link Expiry Updated</b>\n\n"
            f"<blockquote>Duration  →  {exp_str}</blockquote>"
        )
        return


@Client.on_message(
    filters.private & filters.command("cancel") & filters.user(Config.ADMIN),
    group=2,
)
async def cmd_cancel_panel(client: Client, message: Message):
    if _state.pop(message.from_user.id, None):
        await message.reply_text("◈ <b>Cancelled</b>")


async def _refresh_settings(query: CallbackQuery) -> None:
    try:
        await query.message.edit_text(
            await _settings_text(),
            reply_markup=_settings_markup(),
        )
    except Exception:
        pass
