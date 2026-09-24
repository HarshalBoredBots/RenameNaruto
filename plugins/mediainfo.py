"""
plugins/mediainfo.py
─────────────────────
/mi command — Reply to any media file to get full MediaInfo.

Pipeline
────────
  1. Partial download (~15% / max 50 MB) — ffprobe only needs the header.
  2. _ffprobe_sync()  — subprocess inside run_blocking(), never blocks event loop.
  3. _build_telegraph_nodes() — builds a rich HTML node tree matching the format:

       [@BotUsername]  April 24, 2026
       #### MediaInfo of filename.mkv
       ---
       ### 🎬 General Info
       ### 🖼️ Video Stream
       ### 🔊 Audio Tracks      (numbered list)
       ### 📝 Subtitle Tracks   (numbered list)
       ### 🔖 Chapters          (numbered list, if present)
       ### 🛠️ Technical

  4. _upload_to_telegraph() — aiohttp POST, 3-retry, node-based content.
  5. Send Telegraph link, or fall back to inline <code> block.

BOT_USERNAME env var (no @) controls the attribution link at the top.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message

from config import Config
from helper.database import jishubotz
from helper.ffmpeg import run_blocking
from messages import log, Msg

TEMP_DIR = "downloads/mediainfo"

# Module-level Telegraph token — created once, reused across calls
_telegraph_token: str | None = None


# ══════════════════════════════════════════════════════════════════════════════
# /mi command handler
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("mi"))
async def mediainfo_cmd(client: Client, message: Message):
    reply = message.reply_to_message
    if not reply or not reply.media:
        return await message.reply_text(
            "❌ **Reply to a media file** with /mi to get its MediaInfo."
        )

    media = getattr(reply, reply.media.value, None)
    if media is None:
        return await message.reply_text("❌ Unsupported media type.")

    if await jishubotz.is_banned(message.from_user.id):
        return

    status    = await message.reply_text("⏳ Fetching MediaInfo...")
    file_path = None

    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        raw_name  = getattr(media, "file_name", None) or f"mi_{int(time.time())}"
        file_size = getattr(media, "file_size", 0)
        safe_name = "".join(c for c in raw_name if c.isalnum() or c in "._- []@")
        file_path = os.path.join(
            TEMP_DIR,
            f"{message.from_user.id}_{int(time.time())}_{safe_name}",
        )

        # Partial download: first 15% of the file, max 50 MB
        partial_limit = min(int(file_size * 0.15), 50 * 1024 * 1024)
        partial_limit = max(partial_limit, 2 * 1024 * 1024)

        await status.edit(
            f"⏳ Downloading header ({_humanbytes(partial_limit)} of "
            f"{_humanbytes(file_size)})..."
        )

        file_path = await _partial_download(client, reply, file_path, partial_limit)

        if not file_path or not os.path.exists(file_path):
            return await status.edit("❌ Failed to fetch file header.")

        actual_size = os.path.getsize(file_path)
        await status.edit(f"🔍 Analysing streams ({_humanbytes(actual_size)} fetched)...")

        data = await run_blocking(_ffprobe_sync, file_path)

        await status.edit("📤 Uploading to Telegraph...")

        bot_username = getattr(client, "username", None) or "RimuruBot"
        nodes        = _build_telegraph_nodes(data, raw_name, file_size, bot_username)
        page_title   = f"MediaInfo of {raw_name}"
        page_url     = await _upload_to_telegraph(page_title, nodes, bot_username)

        if page_url:
            # Display as clickable "MediaInfo" hyperlink — opens Telegraph
            # instant view directly (same style as graph.org links).
            # Markdown: [text](url)  — Pyrogram parses this with parse_mode=Markdown
            await status.edit(
                f"📊 **MediaInfo**\n\n"
                f"📂 **File:** `{raw_name}`\n"
                f"📦 **Size:** `{_humanbytes(file_size)}`\n\n"
                f"[📋 View Full MediaInfo]({page_url})",
                disable_web_page_preview=True,
            )
        else:
            # Fallback: plain-text inline block
            plain = _build_plain_fallback(data, raw_name, file_size)
            truncated = plain[:3800] + ("\n\n… (truncated)" if len(plain) > 3800 else "")
            await status.edit(f"📊 **MediaInfo**\n\n<code>{truncated}</code>")

    except Exception as e:
        log.error(Msg.MI_ERROR, error=e)
        await status.edit(f"❌ Failed to generate MediaInfo.\n\n`{e}`")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# Partial download
# ══════════════════════════════════════════════════════════════════════════════

async def _partial_download(client, message, dest_path: str, limit_bytes: int) -> str | None:
    try:
        written = 0
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with open(dest_path, "wb") as f:
            async for chunk in client.stream_media(message, limit=limit_bytes):
                f.write(chunk)
                written += len(chunk)
                if written >= limit_bytes:
                    break
        return dest_path if written > 0 else None
    except Exception as e:
        log.warning(Msg.MI_ERROR, error=f"partial download: {e}")
        try:
            return await client.download_media(message, file_name=dest_path)
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════════════════════
# ffprobe — sync subprocess (called via run_blocking)
# ══════════════════════════════════════════════════════════════════════════════

def _ffprobe_sync(file_path: str) -> dict:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        # Increase probesize so attachment-heavy MKVs (many embedded fonts)
        # are fully probed without "unknown codec" warnings that cause
        # stream data to be missing from the mediainfo output.
        "-probesize", "50000000",       # 50 MB (default is 5 MB)
        "-analyzeduration", "10000000", # 10 s in µs (default is 0)
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.stdout:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# Telegraph node builder — matches the target format exactly
#
# Node schema: str | {"tag": str, "attrs"?: dict, "children": list}
#
# Sections produced:
#   [@BotUsername link]  Date
#   #### MediaInfo of filename
#   ---
#   ### 🎬 General Info
#   ### 🖼️ Video Stream
#   ### 🔊 Audio Tracks      (ol > li per track)
#   ### 📝 Subtitle Tracks   (ol > li per track)
#   ### 🔖 Chapters          (ol > li per chapter, if any)
#   ### 🛠️ Technical
# ══════════════════════════════════════════════════════════════════════════════

def _build_telegraph_nodes(
    data: dict,
    display_name: str,
    file_size: int,
    bot_username: str,
) -> list:
    fmt      = data.get("format", {})
    streams  = data.get("streams", [])
    chapters = data.get("chapters", [])
    tags_f   = fmt.get("tags", {})

    from datetime import datetime  # safe to re-import inside function

    # ── Node helpers ──────────────────────────────────────────────────────────
    def h3(text):       return {"tag": "h3", "children": [text]}
    def h4(text):       return {"tag": "h4", "children": [text]}
    def p(*c):          return {"tag": "p",  "children": list(c)}
    def bold(t):        return {"tag": "b",  "children": [t]}
    def em(t):          return {"tag": "em", "children": [t]}
    def code(t):        return {"tag": "code", "children": [t]}
    def br():           return {"tag": "br"}
    def hr():           return {"tag": "hr"}
    def link(url, txt): return {"tag": "a", "attrs": {"href": url}, "children": [txt]}
    def li(*c):         return {"tag": "li", "children": list(c)}
    def ol(items):      return {"tag": "ol", "children": items}

    nodes = []

    # ── Header: bot link + date ───────────────────────────────────────────────
    date_str = datetime.utcnow().strftime("%B %d, %Y")
    nodes.append(p(
        link(f"https://t.me/{bot_username}", f"@{bot_username}"),
        f"  {date_str}",
    ))

    # ── Page title ────────────────────────────────────────────────────────────
    nodes.append(h4(f"MediaInfo of {display_name}"))
    nodes.append(hr())

    # ── 🎬 General Info ───────────────────────────────────────────────────────
    nodes.append(h3("🎬 General Info"))

    fmt_name   = fmt.get("format_long_name") or fmt.get("format_name") or "N/A"
    dur        = float(fmt.get("duration") or 0)
    br_raw     = fmt.get("bit_rate", "")
    title_tag  = tags_f.get("title") or tags_f.get("TITLE") or ""

    # Resolution from first video stream
    res_str = ""
    for s in streams:
        if s.get("codec_type") == "video":
            w = s.get("width"); h_ = s.get("height")
            if w and h_:
                res_str = f"{w}x{h_}"
            break

    if title_tag:
        nodes.append(p(bold("Title: "), title_tag))
    nodes.append(p(bold("Format: "), fmt_name))
    if res_str:
        nodes.append(p(bold("Resolution: "), res_str))
    if dur:
        nodes.append(p(bold("Duration: "), _fmt_dur_long(int(dur))))
    nodes.append(p(bold("File Size: "), _humanbytes(file_size)))
    if br_raw:
        nodes.append(p(bold("Bitrate: "), _fmt_br(br_raw)))

    nodes.append(hr())

    # ── 🖼️ Video Stream ───────────────────────────────────────────────────────
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if video_streams:
        nodes.append(h3("🖼️ Video Stream"))
        s      = video_streams[0]
        stags  = s.get("tags", {})

        codec_name = s.get("codec_name", "?")
        codec_long = s.get("codec_long_name", "")
        profile    = s.get("profile", "")

        # Format: "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (h264) - High"
        codec_str = codec_long if codec_long else codec_name
        if codec_long:
            codec_str += f" ({codec_name})"
        if profile and profile not in ("unknown", ""):
            codec_str += f" - {profile}"

        pix_fmt   = s.get("pix_fmt", "")
        color_sp  = s.get("color_space", "")
        color_pr  = s.get("color_primaries", "")
        color_str = ", ".join(filter(None, [color_sp, color_pr]))
        dar       = s.get("display_aspect_ratio", "")
        rfr       = s.get("r_frame_rate", "")
        avgfr     = s.get("avg_frame_rate", "")
        fps_str   = _parse_fps(rfr) or _parse_fps(avgfr) or ""
        vtitle    = stags.get("title") or stags.get("TITLE") or ""

        if vtitle:
            nodes.append(p(bold("Title: "), vtitle))
        nodes.append(p(bold("Codec: "), codec_str))
        if pix_fmt:
            nodes.append(p(bold("Pixel Format: "), pix_fmt))
        if color_str:
            nodes.append(p(bold("Color: "), color_str))
        if dar and dar != "0:1":
            nodes.append(p(bold("Aspect Ratio: "), dar))
        if fps_str:
            nodes.append(p(bold("Frame Rate: "), fps_str))

        nodes.append(hr())

    # ── 🔊 Audio Tracks ───────────────────────────────────────────────────────
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if audio_streams:
        nodes.append(h3("🔊 Audio Tracks"))
        audio_items = []

        for s in audio_streams:
            stags     = s.get("tags", {})
            lang_raw  = stags.get("language") or stags.get("LANGUAGE") or "Unknown"
            lang      = _lang_display(lang_raw)
            disp      = s.get("disposition", {})
            flags     = []
            if disp.get("default"): flags.append("Default")
            if disp.get("forced"):  flags.append("Forced")
            flag_str  = f" ({', '.join(flags)})" if flags else ""

            codec_long = s.get("codec_long_name", "")
            codec_name = s.get("codec_name", "?")
            codec_str  = codec_long if codec_long else codec_name
            if codec_long:
                codec_str += f" ({codec_name})"

            ch_layout = s.get("channel_layout", "")
            ch_count  = s.get("channels", "")
            ch_str    = ch_layout if ch_layout else (f"{ch_count}ch" if ch_count else "")

            abr    = s.get("bit_rate", "")
            sr     = s.get("sample_rate", "")
            sr_str = f"{int(float(sr)) // 1000}kHz" if sr else ""

            # e.g. "ATSC A/52B (AC-3, E-AC-3) (eac3) stereo @ 224 kbps, 48kHz"
            detail_parts = [codec_str]
            if ch_str:  detail_parts.append(ch_str)
            if abr:     detail_parts.append(f"@ {_fmt_br(abr)}")
            if sr_str:  detail_parts.append(sr_str)
            detail_str = " ".join(detail_parts)

            atitle = stags.get("title") or stags.get("TITLE") or ""

            item_children = [bold(f"{lang}{flag_str} - "), detail_str]
            if atitle:
                item_children += [br(), em(f"  ‣ {atitle}")]

            audio_items.append(li(*item_children))

        nodes.append(ol(audio_items))
        nodes.append(hr())

    # ── 📝 Subtitle Tracks ────────────────────────────────────────────────────
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    if sub_streams:
        nodes.append(h3("📝 Subtitle Tracks"))
        sub_items = []

        for s in sub_streams:
            stags    = s.get("tags", {})
            lang_raw = stags.get("language") or stags.get("LANGUAGE") or "Unknown"
            lang     = _lang_display(lang_raw)
            disp     = s.get("disposition", {})
            flags    = []
            if disp.get("default"): flags.append("Default")
            if disp.get("forced"):  flags.append("Forced")
            flag_str = f" ({', '.join(flags)})" if flags else ""

            codec_long = s.get("codec_long_name", "")
            codec_name = s.get("codec_name", "?")
            codec_str  = codec_long if codec_long else codec_name
            if codec_long:
                codec_str += f" ({codec_name})"

            stitle = stags.get("title") or stags.get("TITLE") or ""

            item_children = [bold(f"{lang}{flag_str} - "), em(codec_str)]
            if stitle:
                item_children += [br(), f"  ‣ {stitle}"]

            sub_items.append(li(*item_children))

        nodes.append(ol(sub_items))
        nodes.append(hr())

    # ── 🔖 Chapters ───────────────────────────────────────────────────────────
    if chapters:
        nodes.append(h3("🔖 Chapters"))
        ch_items = []
        for i, ch in enumerate(chapters, 1):
            ctags = ch.get("tags", {})
            title = ctags.get("title") or ctags.get("TITLE") or f"Chapter {i}"
            start = _fmt_timestamp(float(ch.get("start_time", 0)))
            end   = _fmt_timestamp(float(ch.get("end_time", 0)))
            ch_items.append(li(code(f"{title}:"), f" {start} - {end}"))
        nodes.append(ol(ch_items))
        nodes.append(hr())

    # ── 🛠️ Technical ──────────────────────────────────────────────────────────
    writing_app = (
        tags_f.get("writing_application")
        or tags_f.get("WRITING_APPLICATION")
        or tags_f.get("encoder")
        or tags_f.get("ENCODER")
        or ""
    )
    encoded_by = tags_f.get("encoded_by") or tags_f.get("ENCODED_BY") or ""
    nb_streams = fmt.get("nb_streams", "")

    tech_lines = []
    if writing_app:
        tech_lines.append(p(bold("Muxed with: "), writing_app))
    if encoded_by:
        tech_lines.append(p(bold("Encoded By: "), encoded_by))
    if nb_streams:
        tech_lines.append(p(bold("Total Streams: "), str(nb_streams)))

    if tech_lines:
        nodes.append(h3("🛠️ Technical"))
        nodes.extend(tech_lines)

    return nodes


# ══════════════════════════════════════════════════════════════════════════════
# Plain-text fallback (used when Telegraph upload fails)
# ══════════════════════════════════════════════════════════════════════════════

def _build_plain_fallback(data: dict, display_name: str, file_size: int) -> str:
    fmt     = data.get("format", {})
    streams = data.get("streams", [])
    tags_f  = fmt.get("tags", {})
    lines   = []

    lines.append(f"━━ GENERAL ━━")
    lines.append(f"  Name     : {display_name}")
    lines.append(f"  Size     : {_humanbytes(file_size)}")
    fmt_name = fmt.get("format_long_name") or fmt.get("format_name") or "N/A"
    lines.append(f"  Format   : {fmt_name}")
    dur = float(fmt.get("duration") or 0)
    if dur:
        lines.append(f"  Duration : {_fmt_dur_long(int(dur))}")
    br = fmt.get("bit_rate", "")
    if br:
        lines.append(f"  Bitrate  : {_fmt_br(br)}")
    lines.append("")

    for s in streams:
        ctype = (s.get("codec_type") or "").upper()
        stags = s.get("tags", {})
        lang  = stags.get("language") or stags.get("LANGUAGE") or ""
        codec = s.get("codec_name", "?")

        if ctype == "VIDEO":
            lines.append("━━ VIDEO ━━")
            lines.append(f"  Codec    : {codec}")
            w = s.get("width"); h_ = s.get("height")
            if w and h_:
                lines.append(f"  Res      : {w}x{h_}")
            rfr = s.get("r_frame_rate", "")
            fps = _parse_fps(rfr)
            if fps:
                lines.append(f"  FPS      : {fps}")
            lines.append("")
        elif ctype == "AUDIO":
            lang_disp = _lang_display(lang) if lang else "Unknown"
            disp  = s.get("disposition", {})
            flags = []
            if disp.get("default"): flags.append("Default")
            if disp.get("forced"):  flags.append("Forced")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            abr = s.get("bit_rate", "")
            lines.append(f"  🔊 {lang_disp}{flag_str} — {codec}" + (f" @ {_fmt_br(abr)}" if abr else ""))
        elif ctype == "SUBTITLE":
            lang_disp = _lang_display(lang) if lang else "Unknown"
            disp  = s.get("disposition", {})
            flags = []
            if disp.get("default"): flags.append("Default")
            if disp.get("forced"):  flags.append("Forced")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"  📝 {lang_disp}{flag_str} — {codec}")

    return "\n".join(lines).strip()


# ══════════════════════════════════════════════════════════════════════════════
# Telegraph uploader — nodes version
# ══════════════════════════════════════════════════════════════════════════════

async def _upload_to_telegraph(title: str, nodes: list, bot_username: str) -> str | None:
    global _telegraph_token

    # ── Truncate at node level so the JSON is always valid ────────────────────
    # Slicing the raw JSON string mid-structure produces invalid JSON and causes
    # Telegraph to return a page URL whose content is completely empty.
    # Instead, remove trailing nodes one at a time until the payload fits.
    _TELEGRAPH_LIMIT = 60_000   # bytes; Telegraph hard limit is 64 KB
    safe_nodes = list(nodes)
    while True:
        nodes_json = json.dumps(safe_nodes, ensure_ascii=False)
        if len(nodes_json.encode("utf-8")) <= _TELEGRAPH_LIMIT:
            break
        if not safe_nodes:
            log.warning(Msg.MI_TELEGRAPH_ERR, error="content too large even after node truncation")
            return None
        safe_nodes.pop()

    # ── Step 1: get / reuse account token ────────────────────────────────────
    if not _telegraph_token:
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as session:
                    async with session.post(
                        "https://api.graph.org/createAccount",
                        data={
                            "short_name":  bot_username[:32],
                            "author_name": f"@{bot_username}",
                            "author_url":  f"https://t.me/{bot_username}",
                        },
                    ) as r:
                        d = json.loads(await r.text())
                        if d.get("ok"):
                            _telegraph_token = d["result"]["access_token"]
                            break
                        log.warning(Msg.MI_TELEGRAPH_ERR,
                                    error=f"createAccount attempt {attempt+1}: {d.get('error')}")
            except Exception as exc:
                log.warning(Msg.MI_TELEGRAPH_ERR,
                            error=f"createAccount attempt {attempt+1}: {exc}")
            if attempt < 2:
                await asyncio.sleep(3)

    if not _telegraph_token:
        log.warning(Msg.MI_TELEGRAPH_ERR, error="could not obtain Telegraph token after 3 tries")
        return None

    # ── Step 2: create page ───────────────────────────────────────────────────
    page_title = (title or "MediaInfo")[:256]

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                async with session.post(
                    "https://api.graph.org/createPage",
                    data={
                        "access_token": _telegraph_token,
                        "title":        page_title,
                        "author_name":  f"@{bot_username}",
                        "author_url":   f"https://t.me/{bot_username}",
                        "content":      nodes_json,
                    },
                ) as r:
                    result = json.loads(await r.text())

                    if result.get("ok"):
                        path = result["result"]["path"]
                        return f"https://graph.org/{path}"

                    err = result.get("error", "unknown")
                    log.warning(Msg.MI_TELEGRAPH_ERR,
                                error=f"createPage attempt {attempt+1}: {err}")
                    if "ACCESS_TOKEN" in str(err).upper():
                        _telegraph_token = None
                        break

        except Exception as exc:
            log.warning(Msg.MI_TELEGRAPH_ERR,
                        error=f"createPage attempt {attempt+1}: {exc}")

        if attempt < 2:
            await asyncio.sleep(3)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

# ISO 639-2/3 language code → display name mapping
_LANG_MAP = {
    "jpn": "Japanese", "eng": "English", "ger": "German", "deu": "German",
    "spa": "Castilian / Spanish", "fre": "French", "fra": "French",
    "ita": "Italian", "por": "Portuguese", "tha": "Thai", "ara": "Arabic",
    "hin": "Hindi", "chi": "Chinese", "zho": "Chinese", "kor": "Korean",
    "rus": "Russian", "tur": "Turkish", "pol": "Polish", "dut": "Dutch / Flemish",
    "nld": "Dutch / Flemish", "ind": "Indonesian", "may": "Malay",
    "msa": "Malay", "vie": "Vietnamese", "swe": "Swedish", "nor": "Norwegian",
    "dan": "Danish", "fin": "Finnish", "heb": "Hebrew", "ces": "Czech",
    "cze": "Czech", "slk": "Slovak", "hun": "Hungarian", "ron": "Romanian",
    "rum": "Romanian", "bul": "Bulgarian", "hrv": "Croatian", "srp": "Serbian",
    "ukr": "Ukrainian", "cat": "Catalan",
}


def _lang_display(code: str) -> str:
    """Convert ISO language code to display name, e.g. 'jpn' → 'Japanese'."""
    if not code:
        return "Unknown"
    lower = code.lower().strip()
    return _LANG_MAP.get(lower, code.title())


def _humanbytes(size) -> str:
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "N/A"
    if size <= 0:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def _fmt_dur_long(seconds: int) -> str:
    """e.g. 1456 → '24mins 16s'  or  '1hr 4mins 16s'"""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h: parts.append(f"{h}hr")
    if m: parts.append(f"{m}mins")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)


def _fmt_timestamp(seconds: float) -> str:
    """e.g. 90.5 → '0:01:30'"""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def _fmt_br(br) -> str:
    try:
        br = int(br)
        if br >= 1_000_000:
            return f"{br / 1_000_000:.2f} Mbps"
        if br >= 1_000:
            return f"{br / 1_000:.0f} kbps"
        return f"{br} bps"
    except Exception:
        return str(br)


def _parse_fps(fraction_str: str) -> str:
    try:
        if "/" in fraction_str:
            num, den = fraction_str.split("/")
            val = float(num) / float(den)
            if val <= 0:
                return ""
            for known in (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0, 120.0):
                if abs(val - known) < 0.01:
                    return f"{known:.3f}".rstrip("0").rstrip(".")
            return f"{val:.3f}".rstrip("0").rstrip(".")
        val = float(fraction_str)
        return f"{val:.3f}".rstrip("0").rstrip(".") if val > 0 else ""
    except Exception:
        return ""
