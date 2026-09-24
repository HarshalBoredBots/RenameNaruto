"""
plugins/image_tools.py
───────────────────────
Handles two flows:

1. User sends a PHOTO → show [💾 Save Thumbnail] [🚀 Upscale Image]
   • Thumbnail is NOT saved automatically — user must tap Save Thumbnail.

2. Extract Thumbnail button on file action picker:
   • Step 1: Try Telegram's embedded thumb (instant, no download).
   • Step 2: Partial download (~5 MB header) → ffmpeg frame grab.
   • Never downloads the full file just for a thumbnail.

Upscale engine:
   • Uses waifu2x-ncnn-vulkan (AI upscaling, anime-optimised).
   • Falls back to PIL LANCZOS + sharpening if binary is not found.
   • Daily limit: FREE = 3 upscales/day  |  PREMIUM = unlimited.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from helper.database import jishubotz
from helper.ffmpeg import run_blocking, fix_thumb

# Cache: { action_msg_id: original_photo_file_id }
_photo_cache: dict[int, str] = {}

# Partial download limit for thumbnail extraction
_THUMB_PARTIAL_BYTES = 15 * 1024 * 1024

# Daily upscale limits
_UPSCALE_LIMIT_FREE    = 3
_UPSCALE_LIMIT_PREMIUM = 0   # 0 = unlimited


# ── Waifu2x upscale ──────────────────────────────────────────────────────────
# Engine priority:
#   1. waifu2x-ncnn-vulkan binary  (Termux: pkg install waifu2x-ncnn-vulkan)
#      Subprocess call with -g -1 (CPU mode).
#   2. PIL LANCZOS  — always available, zero deps. Used on Heroku.

_WAIFU2X_BIN: str | None = shutil.which("waifu2x-ncnn-vulkan")
_HAS_WAIFU2X_PY = False   # pip package removed — builds from source, unusable on Heroku


def _upscale_sync(input_path: str, output_path: str, factor: int = 2) -> str:
    """
    Upscale priority: waifu2x-ncnn-py → waifu2x-ncnn-vulkan binary → PIL LANCZOS.
    Always writes a JPEG to output_path and returns it.
    """
    # waifu2x only accepts scale 1/2/4/8/16/32 — clamp user factor
    supported = [1, 2, 4, 8, 16, 32]
    scale     = min(supported, key=lambda x: abs(x - factor))

    # ── 1. waifu2x-ncnn-vulkan binary (Termux) ───────────────────────────────
    if _WAIFU2X_BIN:
        cmd = [
            _WAIFU2X_BIN,
            "-i", input_path,
            "-o", output_path,
            "-n", "1",       # light denoise
            "-s", str(scale),
            "-f", "jpg",
            "-g", "-1",      # force CPU — safe on Mali/old Android & Heroku dynos
            "-j", "1:2:1",   # light threading
        ]
        try:
            result = subprocess.run(
                cmd,
                timeout=120,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass   # fall through to PIL

    # ── 2. PIL LANCZOS fallback (Heroku + anywhere waifu2x binary is absent) ─
    from PIL import Image, ImageEnhance, ImageFilter
    img      = Image.open(input_path).convert("RGB")
    new_size = (img.width * factor, img.height * factor)
    img      = img.resize(new_size, Image.LANCZOS)
    img      = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))
    img      = ImageEnhance.Sharpness(img).enhance(1.2)
    img      = ImageEnhance.Contrast(img).enhance(1.1)
    img.save(output_path, "JPEG", quality=92)
    return output_path


def _get_engine_name() -> str:
    """Human-readable name of whichever engine will actually run."""
    if _HAS_WAIFU2X_PY:
        return "waifu2x-ncnn-py (CPU)"
    if _WAIFU2X_BIN:
        return "waifu2x-ncnn-vulkan (CPU)"
    return "PIL LANCZOS"


# ── Daily limit helpers ───────────────────────────────────────────────────────

async def _check_upscale_limit(user_id: int) -> tuple[bool, int, int]:
    """
    Returns (allowed, used_today, limit).
    limit = 0 means unlimited (premium).
    """
    is_premium = await jishubotz.is_premium(user_id)
    if is_premium:
        used = await jishubotz.get_upscale_uses_today(user_id)
        return True, used, 0   # unlimited

    used  = await jishubotz.get_upscale_uses_today(user_id)
    limit = _UPSCALE_LIMIT_FREE
    return used < limit, used, limit


# ── Flow 1: photo received — show action buttons (NO auto-save) ───────────────

@Client.on_message(filters.private & filters.photo, group=5)
async def on_photo_received(client: Client, message: Message):
    """
    Show Save / Upscale buttons when user sends a photo.
    Thumbnail is NOT saved until user explicitly taps 'Save Thumbnail'.
    """
    file_id = message.photo.file_id
    markup  = InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Save Thumb", callback_data="img_save"),
        InlineKeyboardButton("✨ Upscale Image",  callback_data="img_upscale"),
    ]])
    sent = await message.reply_text(
        "◈ <b>Image Detected</b>\n<i>What would you like to do with this photo?</i>",
        reply_to_message_id=message.id,
        reply_markup=markup,
    )
    _photo_cache[sent.id] = file_id


# ── Callbacks: Save / Upscale ─────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^img_(save|upscale|save_stolen)$"))
async def cb_image_action(client: Client, query: CallbackQuery):
    # query.data is one of: "img_save", "img_upscale", "img_save_stolen"
    # Strip the "img_" prefix to get the real action name.
    # Do NOT use split("_")[1] — "img_save_stolen".split("_")[1] == "save", not "save_stolen".
    action   = query.data[len("img_"):]   # "save" | "upscale" | "save_stolen"
    panel_id = query.message.id
    file_id  = _photo_cache.get(panel_id)

    if not file_id:
        await query.answer("Session expired. Send the photo again.", show_alert=True)
        return

    user_id = query.from_user.id

    if action in ("save", "save_stolen"):
        await query.answer("Saving thumbnail…")

        # For save_stolen: upscale first (if factor > 1) then save.
        # Upscaling on save_stolen also consumes a daily upscale slot.
        final_file_id = file_id
        if action == "save_stolen":
            factor = await jishubotz.get_upscale_factor(user_id)
            if factor > 1:
                # ── Daily limit check ─────────────────────────────────────
                allowed, used, limit = await _check_upscale_limit(user_id)
                if not allowed:
                    await query.answer(
                        f"⚠️ Daily upscale limit reached ({used}/{limit}).\n"
                        "Thumbnail saved without upscaling.",
                        show_alert=True,
                    )
                    # Save original (no upscale) instead of refusing entirely
                    await jishubotz.set_thumbnail(user_id, file_id=file_id)
                    try:
                        await query.message.edit_caption(
                            caption="╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n┃  ✨  Saved (upscale limit reached).\n┃  ➜  /view_thumb to preview.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                        )
                    except Exception:
                        try:
                            await query.message.edit_text(
                                "╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n┃  ✨  Saved (upscale limit reached).\n┃  ➜  /view_thumb to preview.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                            )
                        except Exception:
                            pass
                    _photo_cache.pop(panel_id, None)
                    return

                tmp_dir  = f"downloads/steal_save_{user_id}_{int(time.time() * 1000)}"
                os.makedirs(tmp_dir, exist_ok=True)
                in_path  = f"{tmp_dir}/stolen.jpg"
                out_path = f"{tmp_dir}/stolen_up.jpg"
                engine   = _get_engine_name()
                try:
                    dl = await client.download_media(file_id, file_name=in_path)
                    if dl and os.path.exists(in_path) and os.path.getsize(in_path) > 0:
                        await run_blocking(_upscale_sync, in_path, out_path, factor)
                        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                            sent_up = await client.send_photo(
                                query.message.chat.id,
                                photo=out_path,
                                caption=(
                                    f"◈ <b>Enhancement Complete</b>\n"
                                    f"┌  <b>Engine:</b> {engine}\n"
                                    f"├  <b>Scale:</b>  {factor}× Resolution\n"
                                    f"└  <b>Tuning:</b> Noise Reduction + Sharpness Applied."
                                ),
                            )
                            final_file_id = sent_up.photo.file_id
                            await jishubotz.inc_upscale_uses(user_id)
                except Exception:
                    pass  # fall back to original file_id
                finally:
                    for p in (in_path, out_path):
                        try:
                            if os.path.exists(p):
                                os.remove(p)
                        except Exception:
                            pass
                    try:
                        os.rmdir(tmp_dir)
                    except Exception:
                        pass

        await jishubotz.set_thumbnail(user_id, file_id=final_file_id)
        try:
            await query.message.edit_caption(
                caption="╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n┃  ✨  Thumbnail saved successfully.\n┃  ➜  /view_thumb to preview.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            )
        except Exception:
            try:
                await query.message.edit_text(
                    "╭━━━〔 🖼️ VISUAL CORE 〕━━━╮\n┃  ✨  Thumbnail saved successfully.\n┃  ➜  /view_thumb to preview.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
                )
            except Exception:
                pass
        _photo_cache.pop(panel_id, None)
        return

    # ── Upscale ───────────────────────────────────────────────────────────────
    # Check daily limit before doing any work
    allowed, used, limit = await _check_upscale_limit(user_id)
    if not allowed:
        limit_str = f"{used}/{limit}"
        await query.answer(
            f"⚠️ Daily upscale limit reached ({limit_str}). Upgrade to Premium for unlimited upscales.",
            show_alert=True,
        )
        return

    engine = _get_engine_name()
    await query.answer("Upscaling…")
    ms = await query.message.edit_text("💠 <i>Great Sage acquiring source…</i>")

    tmp_dir  = f"downloads/upscale_{user_id}_{int(time.time() * 1000)}"
    os.makedirs(tmp_dir, exist_ok=True)
    in_path  = f"{tmp_dir}/input.jpg"
    out_path = f"{tmp_dir}/upscaled.jpg"

    try:
        dl = await client.download_media(file_id, file_name=in_path)
        if not dl or not os.path.exists(in_path) or os.path.getsize(in_path) == 0:
            await ms.edit("╭━━━〔 ❌ SKILL FAILED 〕━━━╮\n┃  ⬇️  Download failed. Try again.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
            return

        factor = await jishubotz.get_upscale_factor(user_id)
        await ms.edit(
            f"⚙️ <b>Status:</b> <code>[●●●●○○○]</code> Processing {factor}× Enhancement...\n"
            f"<i>Engine: {engine}</i>"
        )
        await run_blocking(_upscale_sync, in_path, out_path, factor)

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            await ms.edit("╭━━━〔 ❌ UPSCALE FAILED 〕━━━╮\n┃  ⚠️  Image could not be processed.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
            return

        # Count this upscale use
        await jishubotz.inc_upscale_uses(user_id)
        new_used = used + 1

        # Build remaining-uses footer for free users
        if limit > 0:
            remaining = limit - new_used
            uses_footer = f"\n\n<i>🔢 Uses today: {new_used}/{limit}  •  Remaining: {remaining}</i>"
        else:
            uses_footer = "\n\n<i>⭐ Premium — unlimited upscales</i>"

        await ms.delete()
        await client.send_photo(
            query.message.chat.id,
            photo=out_path,
            caption=(
                f"◈ <b>Enhancement Complete</b>\n"
                f"┌  <b>Engine:</b> {engine}\n"
                f"├  <b>Scale:</b>  {factor}× Resolution\n"
                f"└  <b>Tuning:</b> Noise Reduction + Sharpness Applied."
                f"{uses_footer}"
            ),
        )
        _photo_cache.pop(panel_id, None)

    except Exception as e:
        try:
            await ms.edit(f"╭━━━〔 ❌ UPSCALE FAILED 〕━━━╮\n┃  ⚠️  <code>{e}</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        except Exception:
            pass
    finally:
        for f in (in_path, out_path):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass


# ── Steal Thumb ───────────────────────────────────────────────────────────────
# Just sends the embedded thumbnail as a photo — does NOT save it.
# Works on video, document, cbz, pdf — any file Telegram stores a thumb for.
# Logic mirrors the rename thumbnail resolution: iterate document/video/audio
# attributes for .thumbs, download with download_media, normalise with fix_thumb.

@Client.on_callback_query(filters.regex(r"^action_steal_thumb$"))
async def cb_steal_thumb(client: Client, query: CallbackQuery):
    await query.answer("🪄 Stealing…")

    chat_id = query.message.chat.id

    # ── Resolve original file message ─────────────────────────────────────────
    from plugins.file_rename import _file_cache
    file_message = _file_cache.get(query.message.id) or query.message.reply_to_message

    if not file_message or not file_message.media:
        await query.message.edit_text("╭━━━〔 ⚠️ FILE EXPIRED 〕━━━╮\n┃  Send the file again.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        return

    media    = getattr(file_message, file_message.media.value, None)
    filename = getattr(media, "file_name", None) or "file"

    # ── Resolve thumb file_id from all possible media attributes ──────────────
    # Covers: video, document (mkv/mp4 sent as doc), audio, cbz, pdf
    thumb_file_id: str | None = None
    for attr in ("document", "video", "audio", "photo"):
        obj = getattr(file_message, attr, None)
        if not obj:
            continue
        thumbs = getattr(obj, "thumbs", None)
        if thumbs:
            thumb_file_id = thumbs[0].file_id
            break
        # photo objects ARE the image — use their file_id directly
        if attr == "photo" and hasattr(obj, "file_id"):
            thumb_file_id = obj.file_id
            break
    if thumb_file_id is None and media and getattr(media, "thumbs", None):
        thumb_file_id = media.thumbs[0].file_id

    if not thumb_file_id:
        await query.message.edit_text(
            "╭━━━〔 ❌ NO THUMBNAIL 〕━━━╮\n┃  ⚠️  No embedded art found.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )
        return

    ms = await query.message.edit_text("💠 <i>Great Sage extracting art…</i>")

    tmp_dir  = f"downloads/steal_{query.from_user.id}_{int(time.time() * 1000)}"
    os.makedirs(tmp_dir, exist_ok=True)
    raw_path = os.path.join(tmp_dir, "stolen_raw.jpg")

    try:
        # Download — same as rename pipeline does for custom thumbnail
        dl = await client.download_media(thumb_file_id, file_name=raw_path)
        if not dl or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            await ms.edit("╭━━━〔 ❌ SKILL FAILED 〕━━━╮\n┃  ⬇️  Download failed. Try again.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
            return

        # fix_thumb — same normalisation the rename pipeline applies
        _, __, fixed_path = await fix_thumb(raw_path)
        final_path = fixed_path or raw_path

        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            await ms.edit("╭━━━〔 ❌ EXTRACTION FAILED 〕━━━╮\n┃  ⚠️  Thumbnail unreadable.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
            return

        # Send with Save Thumbnail button — tapping it upscales then saves
        await ms.delete()
        sent_stolen = await client.send_photo(
            chat_id,
            photo=final_path,
            caption=(
                f"◈ <b>Thumbnail Extracted</b>\n"
                f"╭━━━〔 🖼️ THUMBNAIL STOLEN 〕━━━╮\n┃  📂  <code>{filename}</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"🪄 <b>Success!</b> The preview has been pulled.\n"
                f"➜ <i>Tap <b>Save Thumbnail</b> to upscale &amp; set as default.</i>"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💾 Save Thumb", callback_data="img_save_stolen"),
            ]]),
        )
        # Cache the stolen photo's file_id under the sent message id
        _photo_cache[sent_stolen.id] = sent_stolen.photo.file_id

    except Exception as e:
        try:
            await ms.edit(f"╭━━━〔 ❌ SYSTEM ERROR 〕━━━╮\n┃  <code>{e}</code>\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        except Exception:
            pass
    finally:
        try:
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except Exception:
                    pass
            os.rmdir(tmp_dir)
        except Exception:
            pass
