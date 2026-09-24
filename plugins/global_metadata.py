"""
plugins/global_metadata.py
════════════════════════════════════════════════════════════════════════════
Owner-only commands for global metadata control.

Commands
────────
/gmeta              — show current global metadata status + fields
/gmeta on           — enable global metadata override
/gmeta off          — disable global metadata override
/gmeta set <field> <value>
                    — set a single global metadata field
                      e.g.  /gmeta set title My Show Title
/gmeta clear        — clear all global metadata field values
                      (does NOT disable — just empties the fields)

Fields supported (same as per-user metadata):
    title  artist  author  comment  audio  video  subtitle

When global metadata is ON:
    • ALL new jobs use the owner-configured fields instead of user fields.
    • User per-field settings are untouched — they return automatically
      when global metadata is turned OFF.
    • Jobs already queued at confirm-time have their metadata snapshot
      captured (see file_rename._pipeline for snapshot logic).

When global metadata is OFF:
    • Each user's own metadata settings apply as usual.
    • No user data is modified.

Owner check: uses Config.ADMIN list — same as all other admin commands.
Storage: bot_settings MongoDB document (_id=0) — survives restarts.
"""

from __future__ import annotations

import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from helper.database import jishubotz

logger = logging.getLogger(__name__)

# Valid field names (must match _META_DEFAULTS keys in database.py)
_VALID_FIELDS = {"title", "artist", "author", "comment", "audio", "video", "subtitle"}

_FIELD_LABELS = {
    "title":    "🏷️  Title",
    "artist":   "🎨  Artist",
    "author":   "✍️  Author",
    "comment":  "💬  Comment",
    "audio":    "🔊  Audio Track",
    "video":    "🎥  Video Track",
    "subtitle": "📝  Subtitle",
}


# ══════════════════════════════════════════════════════════════════════════════
# Panel builder
# ══════════════════════════════════════════════════════════════════════════════

async def _panel_text() -> str:
    enabled = await jishubotz.get_global_metadata_enabled()
    fields  = await jishubotz.get_global_metadata_fields()
    status  = "✅ ON — overrides all users" if enabled else "❌ OFF — users use own settings"

    lines = [
        "╭━━━〔 🌐 GLOBAL METADATA 〕━━━╮",
        f"┃  ⚡  Status  ·  {status}",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<b>💠 Global Fields:</b>",
    ]
    for key, label in _FIELD_LABELS.items():
        val = (fields.get(key) or "").strip()
        display = f"<code>{val}</code>" if val else "<i>—</i>"
        lines.append(f"┃  {label}  ·  {display}")

    lines += [
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
        "",
        "<b>Usage:</b>",
        "  <code>/gmeta on</code>  · <code>/gmeta off</code>",
        "  <code>/gmeta set title My Show Title</code>",
        "  <code>/gmeta clear</code>  (empty fields, keep state)",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# /gmeta  command
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("gmeta") & filters.user(Config.ADMIN))
async def cmd_gmeta(client: Client, message: Message):
    """
    /gmeta              — status panel
    /gmeta on|off       — toggle
    /gmeta set f v      — set field f to value v
    /gmeta clear        — empty all fields
    """
    parts = message.text.strip().split(maxsplit=3)
    sub   = parts[1].lower() if len(parts) > 1 else ""

    # ── /gmeta (no sub-command) → show panel ─────────────────────────────
    if not sub:
        return await message.reply_text(await _panel_text())

    # ── /gmeta on ─────────────────────────────────────────────────────────
    if sub == "on":
        await jishubotz.set_global_metadata_enabled(True)
        logger.info("[gmeta] Owner %s ENABLED global metadata", message.from_user.id)
        return await message.reply_text(
            "╭━━━〔 🌐 GLOBAL METADATA 〕━━━╮\n"
            "┃  ✅  Enabled — all new jobs\n"
            "┃      will use owner metadata.\n"
            "┃  💾  Persisted\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            + await _panel_text()
        )

    # ── /gmeta off ────────────────────────────────────────────────────────
    if sub == "off":
        await jishubotz.set_global_metadata_enabled(False)
        logger.info("[gmeta] Owner %s DISABLED global metadata", message.from_user.id)
        return await message.reply_text(
            "╭━━━〔 🌐 GLOBAL METADATA 〕━━━╮\n"
            "┃  ❌  Disabled — users return\n"
            "┃      to their own settings.\n"
            "┃  💾  Persisted\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    # ── /gmeta clear ──────────────────────────────────────────────────────
    if sub == "clear":
        await jishubotz.clear_global_metadata_fields()
        logger.info("[gmeta] Owner %s CLEARED global metadata fields", message.from_user.id)
        return await message.reply_text(
            "╭━━━〔 🌐 GLOBAL METADATA 〕━━━╮\n"
            "┃  🗑  All fields cleared.\n"
            "┃  ℹ️  Toggle state unchanged.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    # ── /gmeta set <field> <value> ────────────────────────────────────────
    if sub == "set":
        if len(parts) < 4:
            return await message.reply_text(
                "❌ Usage: <code>/gmeta set &lt;field&gt; &lt;value&gt;</code>\n\n"
                f"Valid fields: <code>{', '.join(sorted(_VALID_FIELDS))}</code>"
            )
        field = parts[2].lower()
        value = parts[3].strip()
        if field not in _VALID_FIELDS:
            return await message.reply_text(
                f"❌ Unknown field <code>{field}</code>.\n"
                f"Valid: <code>{', '.join(sorted(_VALID_FIELDS))}</code>"
            )
        await jishubotz.set_global_metadata_field(field, value)
        logger.info("[gmeta] Owner %s SET %s = %r", message.from_user.id, field, value)
        label = _FIELD_LABELS.get(field, field)
        return await message.reply_text(
            f"╭━━━〔 🌐 GLOBAL METADATA 〕━━━╮\n"
            f"┃  ✅  {label}\n"
            f"┃      → <code>{value}</code>\n"
            f"┃  💾  Persisted\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    # ── Unknown sub-command ────────────────────────────────────────────────
    await message.reply_text(
        "╭━━━〔 🌐 GLOBAL METADATA HELP 〕━━━╮\n"
        "┃  /gmeta          ·  show status\n"
        "┃  /gmeta on       ·  enable override\n"
        "┃  /gmeta off      ·  disable override\n"
        "┃  /gmeta set f v  ·  set field f to v\n"
        "┃  /gmeta clear    ·  empty all fields\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"<b>Fields:</b> <code>{', '.join(sorted(_VALID_FIELDS))}</code>"
    )
