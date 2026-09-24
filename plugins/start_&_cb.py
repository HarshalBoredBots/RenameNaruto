"""
plugins/start_&_cb.py  —  /start and general callback handlers.
Rimuru Tempest themed UI.
"""

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from config import Config, Txt
from helper.database import jishubotz
from helper.ui import RUI, KB


# ── Render helper ─────────────────────────────────────────────────────────────

async def _render(client, chat_id, text, markup, *, pic_key, fallback_pic=None, msg=None):
    """Send or edit a message: tries photo caption → text fallback."""
    pic = await jishubotz.get_pic(pic_key, fallback=fallback_pic)

    if msg is not None:
        if pic:
            try:
                await msg.edit_media(InputMediaPhoto(media=pic, caption=text), reply_markup=markup)
                return
            except Exception:
                pass
        try:
            await msg.edit_caption(caption=text, reply_markup=markup)
            return
        except Exception:
            pass
        try:
            await msg.edit_text(text=text, reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception:
            pass
    else:
        if pic:
            try:
                await client.send_photo(chat_id, photo=pic, caption=text, reply_markup=markup)
                return
            except Exception:
                pass
        await client.send_message(chat_id, text=text, reply_markup=markup, disable_web_page_preview=True)


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _start_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔮 About",     callback_data="about"),
            InlineKeyboardButton("📖 Skill Book", callback_data="help"),
        ],
        [
            InlineKeyboardButton("👑 Premium",    callback_data="rimuru_premium"),
            InlineKeyboardButton("💜 Support",    callback_data="donate"),
        ],
        [InlineKeyboardButton("⚡ Dev  ·  @naruto0927", url="https://t.me/naruto0927")],
    ])


def _help_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧬 Metadata",   callback_data="meta"),
            InlineKeyboardButton("🏷️ Prefix",      callback_data="prefix"),
        ],
        [
            InlineKeyboardButton("🏷️ Suffix",      callback_data="suffix"),
            InlineKeyboardButton("📝 Caption",     callback_data="caption"),
        ],
        [
            InlineKeyboardButton("🖼️ Thumbnail",    callback_data="thumbnail"),
            InlineKeyboardButton("⚡ Auto Rename",  callback_data="help_ar"),
        ],
        [InlineKeyboardButton("↩ Return to Base", callback_data="start")],
    ])


def _back_kb(to: str = "help") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩ Return", callback_data=to),
        InlineKeyboardButton("✕ Dismiss", callback_data="close"),
    ]])


def _about_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Source",    url="https://github.com/naruto1427"),
            InlineKeyboardButton("💜 Donate",    callback_data="donate"),
        ],
        [InlineKeyboardButton("↩ Return to Base", callback_data="start")],
    ])


# ── Auto Rename help card ─────────────────────────────────────────────────────

_AR_HELP = (
    "╭━━━〔 🧬 AUTO RENAME SYSTEM 〕━━━╮\n"
    "┃  Template-based automatic renaming\n"
    "┃  powered by Great Sage intelligence.\n"
    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
    "<b>⚡ Setup:</b>\n"
    "┃  1️⃣  /mode  →  Select <b>Auto Rename</b>\n"
    "┃  2️⃣  /autorename  →  Set template\n"
    "┃  3️⃣  /setsource  →  Choose source\n"
    "┃  4️⃣  Send files — Rimuru handles the rest\n\n"
    "<b>💠 Placeholders:</b>\n"
    "┃  <code>{season}</code>   →  Season  (e.g. 2)\n"
    "┃  <code>{episode}</code>  →  Episode (e.g. 07)\n"
    "┃  <code>{quality}</code>  →  Quality (e.g. 1080p)\n"
    "┃  <code>{audio}</code>    →  Audio   (e.g. Hindi)\n\n"
    "<b>📊 Limits:</b>  Free · 30/day  ·  Premium · ∞"
)


# ── /start ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("start"))
async def start(client, message):
    await jishubotz.add_user(client, message)
    await _render(
        client, message.chat.id,
        text=Txt.START_TXT.format(message.from_user.mention),
        markup=_start_kb(),
        pic_key="start_pic",
        fallback_pic=Config.START_PIC,
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────

@Client.on_callback_query()
async def cb_handler(client, query: CallbackQuery):
    data = query.data
    cid  = query.message.chat.id
    msg  = query.message
    uid  = query.from_user.id

    # ── Navigation ────────────────────────────────────────────────────────────

    if data == "start":
        await _render(
            client, cid,
            text=Txt.START_TXT.format(query.from_user.mention),
            markup=_start_kb(),
            pic_key="start_pic",
            fallback_pic=Config.START_PIC,
            msg=msg,
        )

    elif data == "help":
        await _render(
            client, cid,
            text=Txt.HELP_TXT,
            markup=_help_kb(),
            pic_key="help_pic",
            msg=msg,
        )

    elif data == "help_ar":
        await _render(
            client, cid,
            text=_AR_HELP,
            markup=_back_kb("help"),
            pic_key="help_pic",
            msg=msg,
        )

    elif data == "meta":
        await _render(client, cid, text=Txt.SEND_METADATA, markup=_back_kb("help"),
                      pic_key="metadata_pic", msg=msg)

    elif data == "prefix":
        await _render(client, cid, text=Txt.PREFIX, markup=_back_kb("help"),
                      pic_key="help_pic", msg=msg)

    elif data == "suffix":
        await _render(client, cid, text=Txt.SUFFIX, markup=_back_kb("help"),
                      pic_key="help_pic", msg=msg)

    elif data == "caption":
        await _render(client, cid, text=Txt.CAPTION_TXT, markup=_back_kb("help"),
                      pic_key="help_pic", msg=msg)

    elif data == "thumbnail":
        await _render(client, cid, text=Txt.THUMBNAIL_TXT, markup=_back_kb("help"),
                      pic_key="help_pic", msg=msg)

    elif data == "about":
        await _render(client, cid, text=Txt.ABOUT_TXT, markup=_about_kb(),
                      pic_key="about_pic", msg=msg)

    elif data == "donate":
        await _render(client, cid, text=Txt.DONATE_TXT, markup=_back_kb("about"),
                      pic_key="about_pic", msg=msg)

    elif data in ("rimuru_premium", "check_premium_status"):
        # Proxy to /premium command output
        is_prem = await jishubotz.is_premium(uid)
        badge   = "👑 <b>Tempest Elite</b>" if is_prem else "👤 <b>Traveler</b>"
        ugrade  = "" if is_prem else "\n\n➤  Contact @naruto0927 to upgrade  💠"
        try:
            await msg.edit_text(
                f"╭━━━〔 👑 PREMIUM STATUS 〕━━━╮\n"
                f"┃  {badge}\n"
                f"┃  {'∞ All features unlocked' if is_prem else '30 auto renames / day'}\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                f"{ugrade}",
                reply_markup=_back_kb("start"),
            )
        except Exception:
            pass

    elif data == "close":
        await query.answer()
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            return
        except Exception:
            pass

    # ── Ban / unban alerts ────────────────────────────────────────────────────

    elif data.startswith("sendAlert_"):
        parts   = data.split("_", 2)
        user_id = int(parts[1].strip())
        reason  = parts[2] if len(parts) > 2 else "No reason provided"
        try:
            await client.send_message(
                user_id,
                "╭━━━〔 🛡 TEMPEST SYSTEM 〕━━━╮\n"
                "┃  ❌  Account Restricted\n"
                f"┃  ⚠️  Reason: {reason}\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "Contact @naruto0927 to appeal."
            )
            await msg.edit_text(
                RUI.success(f"Ban alert dispatched to <code>{user_id}</code>.")
            )
        except Exception as e:
            await msg.edit_text(RUI.error(f"<code>{e}</code>"))

    elif data.startswith("noAlert_"):
        user_id = int(data.split("_")[1].strip())
        await msg.edit_text(RUI.info(f"<code>{user_id}</code> banned silently."))

    elif data.startswith("sendUnbanAlert_"):
        user_id = int(data.split("_")[1].strip())
        try:
            await client.send_message(
                user_id,
                "╭━━━〔 🛡 TEMPEST SYSTEM 〕━━━╮\n"
                "┃  ✨  Account Restored\n"
                "┃  ⚡  Access granted again\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "Welcome back, traveler."
            )
            await msg.edit_text(
                RUI.success(f"Unban alert sent to <code>{user_id}</code>.")
            )
        except Exception as e:
            await msg.edit_text(RUI.error(f"<code>{e}</code>"))

    elif data.startswith("NoUnbanAlert_"):
        user_id = int(data.split("_")[1].strip())
        await msg.edit_text(RUI.info(f"<code>{user_id}</code> unbanned silently."))

    else:
        await query.message.continue_propagation()
