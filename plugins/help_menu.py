"""
plugins/help_menu.py
─────────────────────
/help — Tiered help menu.
  Regular users  → user commands only
  Admins         → user + admin commands
"""

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from config import Config
from helper.database import jishubotz

# ── User command registry ──────────────────────────────────────────────────────

USER_CMDS = {

    # ── Rename core ────────────────────────────────────────────────────────────
    "mode": (
        "🔀 Rename Mode",
        "◈ <b>Rename Mode</b>\n\n"
        "<i>Switch between Manual and Auto rename.</i>\n\n"
        "➜ <code>/mode</code>  —  Open mode selector\n\n"
        "⬜ <b>Manual</b>  — you type a filename for each file\n"
        "✅ <b>Auto</b>    — bot names files using your template\n\n"
        "<b>Limits (free users):</b>\n"
        "✏️ Manual  →  <b>10 renames / day</b>\n"
        "🤖 Auto    →  <b>30 renames / day</b>\n"
        "💎 Premium →  <b>Unlimited</b> in both modes"
    ),
    "rename": (
        "✏️ Manual Rename",
        "◈ <b>Manual Rename</b>\n\n"
        "<i>Send any file → bot asks for a new filename → tap your upload type.</i>\n\n"
        "Supported output types:\n"
        "📄 Document  ·  🎬 Video  ·  🎵 Audio  ·  📚 CBZ/PDF\n\n"
        "➜ Prefix &amp; suffix auto-applied\n"
        "➜ Custom thumbnail &amp; caption attached\n"
        "➜ Metadata injected if enabled\n\n"
        "<b>Free limit:</b> 10 renames/day  ·  Premium = unlimited"
    ),
    "autorename": (
        "🤖 Auto Template",
        "◈ <b>Auto Rename Template</b>\n\n"
        "<i>Set the naming pattern used in Auto mode.</i>\n\n"
        "<b>Placeholders:</b>\n"
        "<code>{season}</code>   →  Season number   <i>(e.g. 2)</i>\n"
        "<code>{episode}</code>  →  Episode number  <i>(e.g. 7)</i>\n"
        "<code>{quality}</code>  →  Quality tag     <i>(e.g. 1080p)</i>\n"
        "<code>{audio}</code>    →  Audio tag       <i>(e.g. Hindi)</i>\n\n"
        "<b>Example:</b>\n"
        "<code>/autorename My Show S{season}E{episode} [{quality}]</code>\n\n"
        "➜ <code>/autorename</code>             — View current template\n"
        "➜ <code>/autorename &lt;template&gt;</code>  — Save new template"
    ),
    "setsource": (
        "📡 Source",
        "◈ <b>Metadata Source</b>\n\n"
        "<i>Where the bot looks for episode/season/quality info.</i>\n\n"
        "➜ <code>/setsource</code>  —  Open selector\n\n"
        "📄 <b>Filename only</b>   — extract from file name\n"
        "📝 <b>Caption only</b>    — extract from message caption\n"
        "🔀 <b>Both</b>            — caption first, fallback to filename\n\n"
        "<i>Useful when channels send generic filenames like\n"
        "<code>video.mkv</code> but info is in the caption.</i>"
    ),
    "setmedia": (
        "🎞️ Output Type",
        "◈ <b>Auto Rename Output Type</b>\n\n"
        "<i>How renamed files are sent back to you.</i>\n\n"
        "➜ <code>/setmedia</code>  —  Open selector\n\n"
        "📄 <b>Document</b>  —  sent as raw file (default)\n"
        "🎥 <b>Video</b>     —  sent with inline video player\n"
        "🎵 <b>Audio</b>     —  sent with inline audio player"
    ),
    "autoqueue": (
        "📋 Queue",
        "◈ <b>Auto Rename Queue</b>\n\n"
        "<i>Monitor and manage active rename jobs.</i>\n\n"
        "➜ <code>/autoqueue</code>  —  Open live queue panel\n\n"
        "⚙️ <b>How it works:</b>\n"
        "• Jobs run in parallel up to the concurrency limit\n"
        "• Extra files wait silently in a numbered queue\n"
        "• Each job shows position, filename, and status\n"
        "• Cancel any job — queued or in-progress\n"
        "• 🔄 Refresh for live updates\n\n"
        "♻️ <b>Restart-safe:</b> queue is saved to MongoDB —\n"
        "unfinished jobs are restored automatically after a restart.\n\n"
        "<b>Daily cap (free users):</b>\n"
        "🤖 Auto rename  →  <b>30 files / day</b>\n"
        "💎 Premium       →  <b>Unlimited</b>"
    ),

    # ── File settings ──────────────────────────────────────────────────────────
    "thumbnail": (
        "🖼️ Thumbnail",
        "◈ <b>Thumbnail</b>\n\n"
        "➜ Send any <b>photo</b>  — set as thumbnail\n"
        "➜ <code>/view_thumb</code>   — preview current thumbnail\n"
        "➜ <code>/del_thumb</code>    — remove thumbnail\n\n"
        "<i>Thumbnail is attached to every renamed file.</i>\n\n"
        "🪄 <b>Steal Thumb:</b> tap the button on any file\n"
        "to extract its embedded thumbnail and save it."
    ),
    "caption": (
        "📝 Caption",
        "◈ <b>Caption Template</b>\n\n"
        "<b>Variables:</b>\n"
        "<code>{filename}</code>  →  Renamed filename\n"
        "<code>{filesize}</code>  →  File size (human-readable)\n"
        "<code>{duration}</code>  →  Duration\n\n"
        "➜ <code>/set_caption &lt;text&gt;</code>  — Save template\n"
        "➜ <code>/see_caption</code>              — View current\n"
        "➜ <code>/del_caption</code>              — Remove"
    ),
    "prefix": (
        "🏷️ Prefix / Suffix",
        "◈ <b>Prefix &amp; Suffix</b>\n\n"
        "<i>Auto-prepended / appended to every filename.</i>\n\n"
        "➜ <code>/set_prefix &lt;text&gt;</code>  — e.g. <code>@MyChannel -</code>\n"
        "➜ <code>/see_prefix</code>\n"
        "➜ <code>/del_prefix</code>\n\n"
        "➜ <code>/set_suffix &lt;text&gt;</code>  — e.g. <code>[1080p]</code>\n"
        "➜ <code>/see_suffix</code>\n"
        "➜ <code>/del_suffix</code>"
    ),
    "metadata": (
        "⚙️ Metadata",
        "◈ <b>Metadata Injection</b>\n\n"
        "<i>Embed custom tags via FFmpeg stream-copy (no re-encode).</i>\n\n"
        "Supported fields:\n"
        "title · artist · author · comment\n"
        "audio track label · video track label · subtitle label\n\n"
        "➜ <code>/metadata</code>  —  Open metadata panel\n\n"
        "⚠️ Files with 20+ font attachments (e.g. some anime MKVs)\n"
        "use selective stream mapping to avoid OOM — attachments are\n"
        "dropped, all A/V/subtitle streams are kept."
    ),
    "mediainfo": (
        "📊 MediaInfo",
        "◈ <b>MediaInfo Report</b>\n\n"
        "<i>Full stream analysis posted to Telegraph.</i>\n\n"
        "Sections: General · Video · Audio · Subtitles · Chapters · Technical\n\n"
        "➜ Send any file → tap <b>📊 MediaInfo</b>\n"
        "➜ Reply to any file with <code>/mi</code>\n\n"
        "<i>Only the file header is downloaded — fast for large files.</i>"
    ),
    "screenshot": (
        "📸 Screenshot Grid",
        "◈ <b>Screenshot Grid</b>\n\n"
        "<i>Evenly-spaced frame grid from any video.</i>\n\n"
        "➜ Send any video → tap <b>📸 Grid</b>\n\n"
        "Default: <b>6 frames</b>  (admin can change via <code>/set_ss</code>)"
    ),
    "sample": (
        "🎞️ Sample Clip",
        "◈ <b>Sample Clip</b>\n\n"
        "<i>Extract a short preview clip from the middle of any video.</i>\n\n"
        "➜ Send any video → tap <b>🎞️ Sample</b>\n\n"
        "Default: <b>30 seconds</b>  (admin can change via <code>/set_sample</code>)"
    ),
    "dump": (
        "📤 Dump Channel",
        "◈ <b>Dump Channel</b>\n\n"
        "<i>Auto-forward renamed files to a Telegram channel.</i>\n\n"
        "➜ <code>/dump</code>  —  Open settings panel\n\n"
        "Requirements:\n"
        "• Bot must be <b>admin</b> in the target channel\n"
        "• Bot must have <b>Post Messages</b> permission\n\n"
        "Files are forwarded with a log caption:\n"
        "<code>📂 original.mkv  ➜  ✏️ renamed.mkv</code>"
    ),
    "history": (
        "📋 History",
        "◈ <b>Rename History</b>\n\n"
        "<i>Your last 20 renamed files, most recent first.</i>\n\n"
        "➜ <code>/history</code>"
    ),
    "leaderboard": (
        "🏆 Leaderboard",
        "◈ <b>Leaderboard</b>\n\n"
        "<i>Top renamers ranked by volume.</i>\n\n"
        "Filters: Today · Weekly · Monthly · All Time\n\n"
        "➜ <code>/leaderboard</code>"
    ),
    "premium": (
        "💎 Premium",
        "◈ <b>Premium Access</b>\n\n"
        "<b>Free limits:</b>\n"
        "✏️ Manual rename   →  10 files / day\n"
        "🤖 Auto rename     →  30 files / day\n\n"
        "<b>Premium perks:</b>\n"
        "➜ Unlimited manual &amp; auto renames\n"
        "➜ No daily cap — ever\n"
        "➜ Priority queue position\n\n"
        "➜ <code>/premium</code>     — View your status &amp; expiry\n"
        "➜ Contact <b>@naruto0927</b> to upgrade 💎"
    ),
}

# ── Admin command registry ─────────────────────────────────────────────────────

ADMIN_CMDS = {
    "limits": (
        "⚙️ Limits",
        "◈ <b>Rename Queue</b>\n\n"
        "<b>Concurrency:</b>\n"
        "➜ <code>/limit &lt;n&gt;</code>  — set max simultaneous rename jobs\n"
        "➜ <code>/limit</code>      — show current concurrency + queue state\n\n"
        "<b>Daily caps (free users):</b>\n"
        "➜ <code>/setlimit manual &lt;n&gt;</code>  — manual renames/day  <i>(default 10)</i>\n"
        "➜ <code>/setlimit auto &lt;n&gt;</code>    — auto renames/day    <i>(default 30)</i>\n\n"
        "<b>View all:</b>\n"
        "➜ <code>/getlimit</code>  — shows concurrency + daily caps + live usage"
    ),
    "jobs": (
        "🗂️ Active Jobs",
        "◈ <b>Job Monitor</b>\n\n"
        "➜ <code>/jobs</code>  —  List all active rename jobs with user IDs\n\n"
        "<i>Also shows in <code>/getlimit</code> under Active per user.</i>"
    ),
    "broadcast": (
        "📢 Broadcast",
        "◈ <b>Broadcast</b>\n\n"
        "<i>Send a message to all registered users.</i>\n\n"
        "➜ Reply to any message with <code>/broadcast</code>\n\n"
        "Supports text, photos, videos, documents.\n"
        "Failed deliveries are silently skipped."
    ),
    "ban": (
        "🚫 Ban / Unban",
        "◈ <b>User Management</b>\n\n"
        "➜ <code>/ban &lt;user_id&gt; [reason]</code>   — restrict user\n"
        "➜ <code>/unban &lt;user_id&gt;</code>           — restore access\n\n"
        "Banned users see an access-denied message on every file."
    ),
    "premium_adm": (
        "💎 Premium Mgmt",
        "◈ <b>Premium Management</b>\n\n"
        "➜ <code>/addpremium &lt;id&gt; &lt;days&gt;</code>  — grant premium\n"
        "   <i>(days = 0 → lifetime)</i>\n"
        "➜ <code>/removepremium &lt;id&gt;</code>          — revoke premium\n"
        "➜ <code>/checkpremium &lt;id&gt;</code>           — check status &amp; expiry\n"
        "➜ <code>/premiumlist</code>                   — all active premium users"
    ),
    "queue_admin": (
        "📋 Queue Admin",
        "◈ <b>Queue Administration</b>\n\n"
        "➜ <code>/autoqueue</code>   — view live queue (all users)\n"
        "➜ <code>/clearqueue</code>  — cancel ALL pending jobs and wipe the DB queue\n\n"
        "⚠️ <code>/clearqueue</code> is destructive — use only in emergencies.\n"
        "Affected users are <b>not</b> notified automatically."
    ),
    "msettings": (
        "🎛️ Media Config",
        "◈ <b>Media Tool Configuration</b>\n\n"
        "➜ <code>/set_sample &lt;sec&gt;</code>   — sample clip duration  <i>(5–300s)</i>\n"
        "➜ <code>/set_ss &lt;n&gt;</code>          — screenshot grid count  <i>(1–12)</i>\n"
        "➜ <code>/set_upscale &lt;n&gt;</code>     — upscale factor         <i>(2 / 3 / 4×)</i>\n"
        "➜ <code>/media_settings</code>        — full settings panel"
    ),
    "panel": (
        "🖼️ Panel Images",
        "◈ <b>Panel Image Manager</b>\n\n"
        "<i>Set custom images for Start, Help, About, Rename, Metadata panels.</i>\n\n"
        "➜ <code>/panel</code>  —  Open image manager\n\n"
        "Supported keys: <code>start_pic</code> · <code>help_pic</code> ·\n"
        "<code>about_pic</code> · <code>rename_pic</code> · <code>metadata_pic</code>"
    ),
    "restart": (
        "🔄 Restart",
        "◈ <b>Restart Bot</b>\n\n"
        "➜ <code>/restart</code>  —  Graceful restart\n\n"
        "♻️ All pending auto-rename jobs are saved to MongoDB\n"
        "and automatically restored after the bot comes back online."
    ),
}


def _is_admin(uid: int) -> bool:
    return uid in Config.ADMIN


# ── Text / markup helpers ──────────────────────────────────────────────────────

def _main_text(is_admin: bool) -> str:
    suffix = "\n➜ <b>Admin Commands</b> also available below." if is_admin else ""
    return (
        "◈ <b>Help</b>\n\n"
        "<i>Tap a section to explore commands.</i>\n\n"
        "┌  ✏️ Manual rename  —  <b>10/day free</b>  ·  unlimited premium\n"
        "┌  🤖 Auto rename    —  <b>30/day free</b>  ·  unlimited premium\n"
        f"└  Queue is restart-safe (MongoDB backed).{suffix}"
    )


def _main_markup(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("💠 User Commands", callback_data="help_section_user")]]
    if is_admin:
        rows.append([InlineKeyboardButton("🛡 Admin Commands", callback_data="help_section_admin")])
    rows.append([InlineKeyboardButton("✕ Dismiss", callback_data="help_close")])
    return InlineKeyboardMarkup(rows)


def _section_markup(section: str) -> InlineKeyboardMarkup:
    cmds = USER_CMDS if section == "user" else ADMIN_CMDS
    btns = [
        InlineKeyboardButton(label, callback_data=f"help_cmd_{section}_{key}")
        for key, (label, _) in cmds.items()
    ]
    rows = [btns[i:i+2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("↩ Back", callback_data="help_main")])
    return InlineKeyboardMarkup(rows)


def _section_text(section: str) -> str:
    return (
        "◈ <b>User Commands</b>\n\n<i>Tap any entry for details:</i>"
        if section == "user" else
        "◈ <b>Admin Commands</b>\n\n<i>Tap any entry for details:</i>"
    )


def _cmd_text(section: str, key: str) -> str | None:
    cmds  = USER_CMDS if section == "user" else ADMIN_CMDS
    entry = cmds.get(key)
    return entry[1] if entry else None


def _cmd_markup(section: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩ Back", callback_data=f"help_section_{section}")
    ]])


# ── /help command ──────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("help"))
async def cmd_help(client: Client, message: Message):
    uid      = message.from_user.id
    is_admin = _is_admin(uid)
    text     = _main_text(is_admin)
    markup   = _main_markup(is_admin)
    pic      = await jishubotz.get_pic("help_pic")
    if pic:
        try:
            await message.reply_photo(photo=pic, caption=text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=markup)


# ── Callbacks ──────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^help_"))
async def cb_help(client: Client, query: CallbackQuery):
    data     = query.data
    is_admin = _is_admin(query.from_user.id)

    async def _edit(text: str, markup: InlineKeyboardMarkup):
        try:
            if query.message.photo:
                await query.message.edit_caption(caption=text, reply_markup=markup)
            else:
                await query.message.edit_text(text=text, reply_markup=markup)
        except Exception:
            pass

    if data == "help_close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data == "help_main":
        await query.answer()
        await _edit(_main_text(is_admin), _main_markup(is_admin))
        return

    if data in ("help_section_user", "help_section_admin"):
        section = data.split("_")[2]
        if section == "admin" and not is_admin:
            return await query.answer("⛔ Admin only.", show_alert=True)
        await query.answer()
        await _edit(_section_text(section), _section_markup(section))
        return

    if data.startswith("help_cmd_"):
        parts = data.split("_", 3)
        if len(parts) < 4:
            return await query.answer()
        section, key = parts[2], parts[3]
        if section == "admin" and not is_admin:
            return await query.answer("⛔ Admin only.", show_alert=True)
        text = _cmd_text(section, key)
        if not text:
            return await query.answer("Command not found.", show_alert=True)
        await query.answer()
        await _edit(text, _cmd_markup(section))
        return

    await query.answer()
