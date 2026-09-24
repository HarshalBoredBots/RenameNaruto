from pyrogram import Client, filters
from helper.database import jishubotz
from helper.ui import RUI


@Client.on_message(filters.private & filters.command("set_caption"))
async def add_caption(client, message):
    if len(message.command) == 1:
        return await message.reply_text(
            "╭━━━〔 📝 CAPTION ENGINE 〕━━━╮\n"
            "┃  Attach custom text to every file.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "<b>⚡ Variables</b>\n"
            "┃  <code>{filename}</code>  ·  renamed name\n"
            "┃  <code>{filesize}</code>  ·  file size\n"
            "┃  <code>{duration}</code>  ·  duration\n\n"
            "<b>🌌 Usage</b>\n"
            "<blockquote><code>/set_caption {filename}\n"
            "📦 {filesize}  ·  ⏱ {duration}</code></blockquote>"
        )
    caption = message.text.split(" ", 1)[1]
    await jishubotz.set_caption(message.from_user.id, caption=caption)
    await message.reply_text(
        "╭━━━〔 📝 CAPTION SAVED 〕━━━╮\n"
        f"┃  <code>{caption}</code>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "✨ Applied to all renamed files."
    )


@Client.on_message(filters.private & filters.command("del_caption"))
async def delete_caption(client, message):
    caption = await jishubotz.get_caption(message.from_user.id)
    if not caption:
        return await message.reply_text(
            "╭━━━〔 📝 CAPTION ENGINE 〕━━━╮\n"
            "┃  ⚠️  No template is currently set.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "➤  /set_caption  ·  create one"
        )
    await jishubotz.set_caption(message.from_user.id, caption=None)
    await message.reply_text(
        "╭━━━〔 📝 CAPTION ENGINE 〕━━━╮\n"
        "┃  ✨  Template removed.\n"
        "┃  Files will use default metadata.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


@Client.on_message(filters.private & filters.command("see_caption"))
async def see_caption(client, message):
    caption = await jishubotz.get_caption(message.from_user.id)
    if caption:
        await message.reply_text(
            "╭━━━〔 📝 CAPTION ENGINE 〕━━━╮\n"
            "┃  <b>Current Template:</b>\n"
            f"┃  <code>{caption}</code>\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "<i>Applied to all renamed files.</i>"
        )
    else:
        await message.reply_text(
            "╭━━━〔 📝 CAPTION ENGINE 〕━━━╮\n"
            "┃  ⚠️  No template set.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "➤  /set_caption  ·  configure one"
        )
