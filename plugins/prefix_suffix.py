"""plugins/prefix_suffix.py — Prefix and suffix command handlers."""

from pyrogram import Client, filters
from pyrogram.types import Message
from helper.database import jishubotz
from helper.ui import RUI


def _card(skill: str, value: str | None, action: str) -> str:
    icon = "🏷️"
    return (
        f"╭━━━〔 {icon} {skill.upper()} SKILL 〕━━━╮\n"
        f"┃  <b>Status:</b>  {'<code>' + value + '</code>' if value else '⚠️  Not set'}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"<i>{action}</i>"
    )


# ── PREFIX ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("set_prefix"))
async def set_prefix(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "╭━━━〔 🏷️ PREFIX SKILL 〕━━━╮\n"
            "┃  Prepended to every filename stem.\n"
            "┃  Works in Manual & Auto mode.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "➤  <b>Usage:</b>  <code>/set_prefix @Channel</code>"
        )
    prefix = message.text.split(None, 1)[1].strip()
    await jishubotz.set_prefix(message.from_user.id, prefix)
    await message.reply_text(
        "╭━━━〔 🏷️ PREFIX ACQUIRED 〕━━━╮\n"
        f"┃  <code>{prefix}</code>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "✨ Applied to all renamed files."
    )


@Client.on_message(filters.private & filters.command("see_prefix"))
async def see_prefix(client: Client, message: Message):
    v = await jishubotz.get_prefix(message.from_user.id)
    await message.reply_text(_card("Prefix", v, "➤  /set_prefix  ·  /del_prefix"))


@Client.on_message(filters.private & filters.command("del_prefix"))
async def del_prefix(client: Client, message: Message):
    if not await jishubotz.get_prefix(message.from_user.id):
        return await message.reply_text(
            RUI.warn("No prefix is currently set.")
        )
    await jishubotz.set_prefix(message.from_user.id, None)
    await message.reply_text(
        "╭━━━〔 🏷️ PREFIX SKILL 〕━━━╮\n"
        "┃  ✨  Prefix cleared.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


# ── SUFFIX ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("set_suffix"))
async def set_suffix(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "╭━━━〔 🏷️ SUFFIX SKILL 〕━━━╮\n"
            "┃  Appended before file extension.\n"
            "┃  Works in Manual & Auto mode.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "➤  <b>Usage:</b>  <code>/set_suffix [720p]</code>"
        )
    suffix = message.text.split(None, 1)[1].strip()
    await jishubotz.set_suffix(message.from_user.id, suffix)
    await message.reply_text(
        "╭━━━〔 🏷️ SUFFIX ACQUIRED 〕━━━╮\n"
        f"┃  <code>{suffix}</code>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "✨ Applied to all renamed files."
    )


@Client.on_message(filters.private & filters.command("see_suffix"))
async def see_suffix(client: Client, message: Message):
    v = await jishubotz.get_suffix(message.from_user.id)
    await message.reply_text(_card("Suffix", v, "➤  /set_suffix  ·  /del_suffix"))


@Client.on_message(filters.private & filters.command("del_suffix"))
async def del_suffix(client: Client, message: Message):
    if not await jishubotz.get_suffix(message.from_user.id):
        return await message.reply_text(RUI.warn("No suffix is currently set."))
    await jishubotz.set_suffix(message.from_user.id, None)
    await message.reply_text(
        "╭━━━〔 🏷️ SUFFIX SKILL 〕━━━╮\n"
        "┃  ✨  Suffix cleared.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
