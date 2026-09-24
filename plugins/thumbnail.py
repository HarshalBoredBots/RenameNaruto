"""plugins/thumbnail.py — Thumbnail management commands."""

from pyrogram import Client, filters
from helper.database import jishubotz


@Client.on_message(filters.private & filters.command(["view_thumb", "viewthumb"]))
async def cmd_view_thumb(client, message):
    thumb = await jishubotz.get_thumbnail(message.from_user.id)
    if thumb:
        try:
            await client.send_photo(
                chat_id=message.chat.id,
                photo=thumb,
                caption=(
                    "╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n"
                    "┃  ✨  Thumbnail  ·  Active\n"
                    "┃  Applied to all renamed files.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "➤  /del_thumb  ·  remove"
                ),
            )
            return
        except Exception:
            await jishubotz.set_thumbnail(message.from_user.id, file_id=None)

    await message.reply_text(
        "╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n"
        "┃  ⚠️  Thumbnail  ·  Not set\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Send any <b>photo</b> and tap <b>Save Thumbnail</b>."
    )


@Client.on_message(filters.private & filters.command(["del_thumb", "delthumb"]))
async def cmd_del_thumb(client, message):
    await jishubotz.set_thumbnail(message.from_user.id, file_id=None)
    await message.reply_text(
        "╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n"
        "┃  ✨  Thumbnail cleared.\n"
        "┃  Files will use original cover art.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
