"""
plugins/user_settings.py
─────────────────────────
/dump — Dump channel configuration panel.
"""

from pyrogram import Client, filters
from pyrogram.errors import (
    ChatAdminRequired, ChannelInvalid,
    PeerIdInvalid, UserNotParticipant, MessageNotModified,
)
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from config import Config
from helper.database import jishubotz
from messages import log, Msg

user_states: dict[int, str] = {}


def _icon(state: bool) -> str:
    return "✅" if state else "❌"


async def _build_markup(user_id: int) -> InlineKeyboardMarkup:
    try:
        s = await jishubotz.get_user_settings(user_id)
    except Exception as e:
        log.error(Msg.US_REFRESH_ERR, error=e)
        s = {"dump_channel": None, "dump_mode": False}

    dump_ch  = s.get("dump_channel")
    ch_label = f"📡 Channel: {dump_ch}" if dump_ch else "📡 Set Dump Channel"
    dm_icon  = "🟢" if s.get("dump_mode", False) else "🔴"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ch_label, callback_data="us_set_dump")],
        [
            InlineKeyboardButton("⚡ Dump Mode", callback_data="us_noop"),
            InlineKeyboardButton(f"{dm_icon} {'On' if s.get('dump_mode') else 'Off'}", callback_data="us_toggle_dump"),
        ],
        [InlineKeyboardButton("✕ Dismiss", callback_data="us_close")],
    ])


def _caption(mention: str) -> str:
    return (
        f"╭━━━〔 📤 DUMP CHANNEL 〕━━━╮\n"
        f"┃  👤  {mention}\n"
        f"┣━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"┃  📡  Set a channel to receive files\n"
        f"┃  🛡   Bot must be admin (Post Messages)\n"
        f"┃  ⚡  Toggle Dump Mode to activate\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


@Client.on_message(filters.private & filters.command("dump"))
async def user_settings_cmd(client: Client, message: Message):
    user   = message.from_user
    markup = await _build_markup(user.id)
    text   = _caption(user.mention)
    pic    = await jishubotz.get_pic("dump_pic", fallback=Config.SETTINGS_IMAGE)

    if pic:
        try:
            return await message.reply_photo(photo=pic, caption=text, reply_markup=markup)
        except Exception as e:
            log.warning(Msg.US_PHOTO_FAIL, error=e)

    await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^us_"), group=1)
async def us_callback_handler(client: Client, query: CallbackQuery):
    data    = query.data
    user_id = query.from_user.id

    if data == "us_noop":
        await query.answer()
        return

    if data == "us_close":
        user_states.pop(user_id, None)
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.message.edit_text("◈ Settings closed.")
            except Exception:
                pass
        return

    if data == "us_set_dump":
        await query.answer()
        user_states[user_id] = "waiting_dump"
        await query.message.reply_text(
            "╭━━━〔 📡 SET DUMP CHANNEL 〕━━━╮\n"
            "┃  Forward a message OR send\n"
            "┃  the channel @username / ID.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n"
            "<blockquote>"
            "→ Forward any message from your channel\n"
            "→ Send the channel ID  <i>(starts with -100)</i>"
            "</blockquote>\n\n"
            "Bot must be admin with Post Messages permission.\n\n"
            "<i>Send /cancel to abort.</i>",
            disable_web_page_preview=True,
        )
        return

    if data == "us_toggle_dump":
        try:
            current = await jishubotz.get_dump_mode(user_id)
            if not current:
                dump_channel = await jishubotz.get_dump_channel(user_id)
                if not dump_channel:
                    await query.answer("⚠️ Set a channel first.", show_alert=True)
                    return
            await jishubotz.set_dump_mode(user_id, not current)
            await query.answer("Dump Mode " + ("enabled ✅" if not current else "disabled ❌"))
            markup = await _build_markup(user_id)
            try:
                await query.message.edit_reply_markup(reply_markup=markup)
            except MessageNotModified:
                pass
        except Exception as e:
            log.error(Msg.US_REFRESH_ERR, error=e)
            await query.answer("Something went wrong.", show_alert=True)
        return

    await query.answer()


@Client.on_message(filters.private & (filters.text | filters.forwarded), group=-1)
async def handle_dump_input(client: Client, message: Message):
    user_id = message.from_user.id
    if user_states.get(user_id) != "waiting_dump":
        return

    if message.text and message.text.strip().lower() in ("/cancel", "cancel"):
        user_states.pop(user_id, None)
        await message.reply_text(
            "◈ <b>Cancelled</b>\n\nNo changes were made."
        )
        return

    channel_id = None

    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
    elif message.text:
        raw = message.text.strip()
        try:
            channel_id = int(raw)
        except ValueError:
            try:
                chat = await client.get_chat(raw)
                channel_id = chat.id
            except Exception:
                await message.reply_text(
                    "❌ Could not resolve that channel.\n"
                    "Forward a message from it or send the numeric ID."
                )
                return

    if channel_id is None:
        await message.reply_text("❌ No channel ID detected.")
        return

    if not str(channel_id).startswith("-100"):
        await message.reply_text("❌ Channel IDs must start with <code>-100</code>.")
        return

    valid, reason = await _check_bot_admin(client, channel_id)
    if not valid:
        await message.reply_text(f"❌ <b>Cannot use this channel.</b>\n\n{reason}")
        return

    try:
        await jishubotz.set_dump_channel(user_id, channel_id)
    except Exception as e:
        await message.reply_text(f"❌ Failed to save.\n\n<code>{e}</code>")
        return

    user_states.pop(user_id, None)
    await message.reply_text(
        f"◈ <b>Dump Channel Saved</b>\n\n"
        f"<blockquote>Channel  →  <code>{channel_id}</code></blockquote>\n\n"
        f"✅ Use /dump to enable Dump Mode."
    )


async def _check_bot_admin(client: Client, channel_id: int) -> tuple[bool, str]:
    try:
        me     = await client.get_chat_member(channel_id, "me")
        status = me.status.value
        if status not in ("administrator", "creator"):
            return False, "Bot is not an admin in that channel."
        if status == "administrator":
            if not (me.privileges and me.privileges.can_post_messages):
                return False, "Bot lacks Post Messages permission."
        return True, ""
    except ChatAdminRequired:
        return False, "Bot needs admin rights."
    except (ChannelInvalid, PeerIdInvalid):
        return False, "Invalid channel. Make sure the bot is a member."
    except UserNotParticipant:
        return False, "Bot is not in that channel."
    except Exception as e:
        return False, f"Unexpected error: {e}"
