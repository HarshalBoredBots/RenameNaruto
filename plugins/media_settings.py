"""
plugins/media_settings.py
──────────────────────────
/media_settings — Inline media configuration panel for admins.

Settings managed here:
  🎞  Sample clip duration   (5 / 15 / 30 / 60 / 120 / 300s)
  📸  Screenshot grid frames (4 / 6 / 9 / 12)
  ✨  AI upscale factor      (2× / 3× / 4×)
  🔗  File-to-link toggle    (enable / disable)
  🖼  Steal thumb toggle     (auto-upscale on / off)

Commands (still work via text):
  /set_sample   <sec>   — 5–300
  /set_ss       <n>     — 1–12
  /set_upscale  <n>     — 2/3/4
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from config import Config
from helper.database import jishubotz


# ══ Text builder ══════════════════════════════════════════════════════════════

async def _settings_text(user_id: int) -> str:
    dur    = await jishubotz.get_sample_duration(user_id)
    count  = await jishubotz.get_screenshot_count(user_id)
    factor = await jishubotz.get_upscale_factor(user_id)
    return (
        "╭━━━〔 ⚙️ MEDIA SETTINGS 〕━━━╮\n"
        f"┃  🎞️  Sample Clip   ·  <code>{dur}s</code>\n"
        f"┃  📸  Grid Frames   ·  <code>{count} frames</code>\n"
        f"┃  ✨  AI Upscale    ·  <code>{factor}×</code>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<i>⚡ Tap any value to change it instantly.</i>"
    )


# ══ Markup builder ════════════════════════════════════════════════════════════

def _settings_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        # Sample duration
        [
            InlineKeyboardButton("🎞 5s",   callback_data="ms_sample_5"),
            InlineKeyboardButton("🎞 15s",  callback_data="ms_sample_15"),
            InlineKeyboardButton("🎞 30s",  callback_data="ms_sample_30"),
            InlineKeyboardButton("🎞 60s",  callback_data="ms_sample_60"),
            InlineKeyboardButton("🎞 120s", callback_data="ms_sample_120"),
            InlineKeyboardButton("🎞 300s", callback_data="ms_sample_300"),
        ],
        # Screenshot frames
        [
            InlineKeyboardButton("📸 4",  callback_data="ms_ss_4"),
            InlineKeyboardButton("📸 6",  callback_data="ms_ss_6"),
            InlineKeyboardButton("📸 9",  callback_data="ms_ss_9"),
            InlineKeyboardButton("📸 12", callback_data="ms_ss_12"),
        ],
        # AI upscale
        [
            InlineKeyboardButton("✨ 2×", callback_data="ms_up_2"),
            InlineKeyboardButton("✨ 3×", callback_data="ms_up_3"),
            InlineKeyboardButton("✨ 4×", callback_data="ms_up_4"),
        ],
        # Navigation
        [
            InlineKeyboardButton("🛡 Admin Panel",  callback_data="ms_open_panel"),
            InlineKeyboardButton("✕ Dismiss",        callback_data="ms_close"),
        ],
    ])


# ══ /media_settings command ═══════════════════════════════════════════════════

@Client.on_message(
    filters.private
    & filters.command(["media_settings", "msettings"])
    & filters.user(Config.ADMIN)
)
async def cmd_media_settings(client: Client, message: Message):
    await message.reply_text(
        await _settings_text(message.from_user.id),
        reply_markup=_settings_markup(),
    )


# ══ Callback handler ══════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^ms_"))
async def cb_media_settings(client: Client, query: CallbackQuery):
    data    = query.data
    user_id = query.from_user.id

    if data == "ms_close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data == "ms_open_panel":
        await query.answer()
        from plugins.panel import _settings_text as pnl_txt, _settings_markup as pnl_mkp
        await query.message.reply_text(
            await pnl_txt(),
            reply_markup=pnl_mkp(),
        )
        return

    # Parse: ms_{kind}_{value}
    parts = data.split("_", 2)
    if len(parts) != 3:
        await query.answer()
        return

    _, kind, raw = parts

    if kind == "sample":
        if not raw.isdigit():
            await query.answer()
            return
        val = int(raw)
        if not 5 <= val <= 300:
            await query.answer("❌ Range: 5–300s", show_alert=True)
            return
        await jishubotz.set_sample_duration(user_id, val)
        await query.answer(f"✅ Sample → {val}s")

    elif kind == "ss":
        if not raw.isdigit():
            await query.answer()
            return
        val = int(raw)
        if not 1 <= val <= 12:
            await query.answer("❌ Range: 1–12", show_alert=True)
            return
        await jishubotz.set_screenshot_count(user_id, val)
        await query.answer(f"✅ Frames → {val}")

    elif kind == "up":
        if not raw.isdigit():
            await query.answer()
            return
        val = int(raw)
        if val not in (2, 3, 4):
            await query.answer("❌ Allowed: 2, 3, 4", show_alert=True)
            return
        await jishubotz.set_upscale_factor(user_id, val)
        await query.answer(f"✅ Upscale → {val}×")

    else:
        await query.answer()
        return

    try:
        await query.message.edit_text(
            await _settings_text(user_id),
            reply_markup=_settings_markup(),
        )
    except Exception:
        pass


# ══ Text commands (still usable) ══════════════════════════════════════════════

@Client.on_message(
    filters.private & filters.command("set_sample") & filters.user(Config.ADMIN)
)
async def cmd_set_sample(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply_text(
            "◈ <b>Set Sample Duration</b>\n\n"
            "<b>Usage:</b> <code>/set_sample [seconds]</code>\n"
            "<i>Range: 5–300 seconds. Default: 30.</i>"
        )
    val = int(parts[1])
    if not 5 <= val <= 300:
        return await message.reply_text("❌ Value must be between 5 and 300 seconds.")
    await jishubotz.set_sample_duration(message.from_user.id, val)
    await message.reply_text(
        f"◈ <b>Sample Duration Updated</b>\n\n"
        f"<blockquote>Duration  →  <code>{val}s</code></blockquote>"
    )


@Client.on_message(
    filters.private & filters.command("set_ss") & filters.user(Config.ADMIN)
)
async def cmd_set_ss(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply_text(
            "◈ <b>Set Screenshot Count</b>\n\n"
            "<b>Usage:</b> <code>/set_ss [count]</code>\n"
            "<i>Range: 1–12 frames. Default: 6.</i>"
        )
    val = int(parts[1])
    if not 1 <= val <= 12:
        return await message.reply_text("❌ Value must be between 1 and 12.")
    await jishubotz.set_screenshot_count(message.from_user.id, val)
    await message.reply_text(
        f"◈ <b>Screenshot Count Updated</b>\n\n"
        f"<blockquote>Frames  →  <code>{val}</code></blockquote>"
    )


@Client.on_message(
    filters.private & filters.command("set_upscale") & filters.user(Config.ADMIN)
)
async def cmd_set_upscale(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply_text(
            "◈ <b>Set Upscale Factor</b>\n\n"
            "<b>Usage:</b> <code>/set_upscale [factor]</code>\n"
            "<i>Allowed: 2, 3, 4. Default: 2.</i>"
        )
    val = int(parts[1])
    if val not in (2, 3, 4):
        return await message.reply_text("❌ Factor must be 2, 3, or 4.")
    await jishubotz.set_upscale_factor(message.from_user.id, val)
    await message.reply_text(
        f"◈ <b>Upscale Factor Updated</b>\n\n"
        f"<blockquote>Factor  →  <code>{val}×</code></blockquote>"
    )
