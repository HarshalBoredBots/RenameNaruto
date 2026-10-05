"""
config.py  —  Bot configuration and all UI text strings.
"""

import os


class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    API_ID       = int(os.environ.get("API_ID", "20140875"))
    API_HASH     = os.environ.get("API_HASH", "a06fa97d5a853ec2da79015b11335a17")
    BOT_TOKEN    = os.environ.get("BOT_TOKEN", "8893679528:AAGFo8bxDDcH9ScocDNyFzK_oxsUukQ_cLs")
    ADMIN        = list(map(int, os.environ.get("ADMIN", "1828405916").split()))

    # ── Storage ───────────────────────────────────────────────────────────────
    DB_URL       = os.environ.get("DB_URL", "mongodb+srv://narutoclaude68_db_user:PZV9ZzKknvXpjYlQ@cluster0.wz6wl5t.mongodb.net/?appName=Cluster0")
    DB_NAME      = os.environ.get("DB_NAME", "RenameBot")
    LOG_CHANNEL  = int(os.environ.get("LOG_CHANNEL", "-1004303151595"))
    BIN_CHANNEL  = int(os.environ.get("BIN_CHANNEL", "-1004373712196"))

    # ── Media ─────────────────────────────────────────────────────────────────
    START_PIC      = os.environ.get("START_PIC", "https://ibb.co/FbxMWCXL")
    SETTINGS_IMAGE = os.environ.get("SETTINGS_IMAGE", "https://ibb.co/cXNV9nms")
    FORCE_SUB      = os.environ.get("FORCE_SUB", "")
    LINK_EXPIRY    = int(os.environ.get("LINK_EXPIRY", "0"))  # 0 = no expiry

    # ── Features ──────────────────────────────────────────────────────────────
    UPSCALE_DAILY_FREE = 3
    BOT_UPTIME         = __import__('time').time()   # set at import time

    # ── Large-file userbot (optional) ─────────────────────────────────────────
    STRING_SESSION = os.environ.get("STRING_SESSION", "")
    BOT_MAX_SIZE   = 2000 * 1024 * 1024   # 2 GB — standard bot limit
    USER_MAX_SIZE  = 4000 * 1024 * 1024   # 4 GB — userbot / TG Premium limit


# ──────────────────────────────────────────────────────────────────────────────
# Txt  —  Rimuru Tempest themed UI strings
# ──────────────────────────────────────────────────────────────────────────────

class Txt:

    # ── /start ────────────────────────────────────────────────────────────────
    START_TXT = (
        "╭━━━〔 💠 RIMURU SYSTEM 💠 〕━━━╮\n"
        "┃  👤  Traveler: <b>{}</b>\n"
        "┃  ⚡  Status: <b>Active</b>\n"
        "┃  🌌  Core: <b>Tempest Online</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<i>Greetings. I am the Rimuru System — your intelligent\n"
        "file management assistant powered by Great Sage.\n\n"
        "Send any file to begin your evolution.</i>\n\n"
        "✨ <b>Tempest File Engine</b>  ·  Ready"
    )

    # ── About ─────────────────────────────────────────────────────────────────
    ABOUT_TXT = (
        "╭━━━〔 🔮 GREAT SAGE 〕━━━╮\n"
        "┃  🧬  Runtime   ·  Python 3.12\n"
        "┃  ⚡  Framework ·  Pyrogram\n"
        "┃  🌌  Database  ·  MongoDB\n"
        "┃  💠  Architect ·  @naruto0927\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<i>This system channels the wisdom of Great Sage\n"
        "and the power of Rimuru Tempest.</i>"
    )

    # ── Help ──────────────────────────────────────────────────────────────────
    HELP_TXT = (
        "╭━━━〔 📖 SKILL LIBRARY 〕━━━╮\n"
        "┃  Select a skill module below\n"
        "┃  to view its usage and power.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<i>Great Sage is standing by to assist.</i>"
    )

    # ── Thumbnail ─────────────────────────────────────────────────────────────
    THUMBNAIL_TXT = (
        "╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n"
        "┃  🖼️  Thumbnail  ·  Configurable\n"
        "┃  ⚡  Applied to all renamed files\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "➤  Send any <b>photo</b> to set a new thumbnail\n"
        "➤  /view_thumb  ·  preview current\n"
        "➤  /del_thumb   ·  remove current\n\n"
        "<i>If none is set, original file art is used.</i>"
    )

    # ── Caption ───────────────────────────────────────────────────────────────
    CAPTION_TXT = (
        "╭━━━〔 📝 CAPTION ENGINE 〕━━━╮\n"
        "┃  Attach custom text to every\n"
        "┃  renamed file automatically.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<b>⚡ Variables</b>\n"
        "┃  <code>{filename}</code>  ·  renamed filename\n"
        "┃  <code>{filesize}</code>  ·  file size\n"
        "┃  <code>{duration}</code>  ·  media duration\n\n"
        "<b>🌌 Example</b>\n"
        "<blockquote><code>📂 {filename}\n"
        "📦 {filesize}  ·  ⏱ {duration}</code></blockquote>"
    )

    # ── Prefix ────────────────────────────────────────────────────────────────
    PREFIX = (
        "╭━━━〔 🏷️ PREFIX SKILL 〕━━━╮\n"
        "┃  Text prepended to filename stem\n"
        "┃  Works in both Manual & Auto mode\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "➤  /set_prefix  @Channel\n"
        "➤  /see_prefix\n"
        "➤  /del_prefix"
    )

    # ── Suffix ────────────────────────────────────────────────────────────────
    SUFFIX = (
        "╭━━━〔 🏷️ SUFFIX SKILL 〕━━━╮\n"
        "┃  Text appended before extension\n"
        "┃  Works in both Manual & Auto mode\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "➤  /set_suffix  [720p]\n"
        "➤  /see_suffix\n"
        "➤  /del_suffix"
    )

    # ── Metadata ──────────────────────────────────────────────────────────────
    SEND_METADATA = (
        "╭━━━〔 🧬 METADATA ENGINE 〕━━━╮\n"
        "┃  Embed custom tags via FFmpeg\n"
        "┃  stream-copy on every rename.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<b>⚡ Fields:</b>  title · artist · author\n"
        "comment · audio · video · subtitle\n\n"
        "➤  /metadata  ·  open configuration panel\n\n"
        "<i>Contact @naruto0927 for help.</i>"
    )

    # ── Progress bar ──────────────────────────────────────────────────────────
    PROGRESS_BAR = (
        "\n"
        "<b>📦  Size   </b>  {1} / {2}\n"
        "<b>⚡  Done   </b>  {0}%\n"
        "<b>🌌  Speed  </b>  {3}/s\n"
        "<b>⏱  ETA    </b>  {4}\n"
    )

    # ── Donate ────────────────────────────────────────────────────────────────
    DONATE_TXT = (
        "╭━━━〔 💜 SUPPORT TEMPEST 〕━━━╮\n"
        "┃  Keep the Rimuru System running\n"
        "┃  and evolving with your support.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "💠  UPI  ·  <code>Narutoprit@fam</code>\n\n"
        "<i>Every contribution fuels the magicules.\n"
        "Thank you, traveler ♡</i>"
    )
