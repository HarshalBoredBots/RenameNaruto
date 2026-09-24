"""
plugins/force_subs.py
──────────────────────
Force-subscribe gate. If FORCE_SUB is set in config,
users must join that channel before using the bot.
"""

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import UserNotParticipant

from config import Config
from helper.database import jishubotz


async def not_subscribed(_, client, message):
    await jishubotz.add_user(client, message)
    if not Config.FORCE_SUB:
        return False
    try:
        user = await client.get_chat_member(Config.FORCE_SUB, message.from_user.id)
        if user.status == enums.ChatMemberStatus.BANNED:
            return True
        return False
    except UserNotParticipant:
        pass
    return True


@Client.on_message(filters.private & filters.create(not_subscribed))
async def force_sub(client, message):
    if not Config.FORCE_SUB:
        return

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌌 Enter Tempest", url=f"https://t.me/{Config.FORCE_SUB}")
    ]])

    try:
        member = await client.get_chat_member(Config.FORCE_SUB, message.from_user.id)
        if member.status == enums.ChatMemberStatus.BANNED:
            return await message.reply_text(
                "◈ <b>Access Denied</b>\n\n"
                "⛔ You are banned from using this bot.\n"
                "Contact @naruto0927 for support."
            )
    except UserNotParticipant:
        pass

    await message.reply_text(
        f"╭━━━〔 🌌 TEMPEST GATE 〕━━━╮\n"
        f"┃  👤  {message.from_user.mention}\n"
        f"┃  ⚡  Join our channel first.\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
        reply_markup=markup,
    )
