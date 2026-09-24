"""
plugins/premium.py
───────────────────
Premium user system.

Admin commands:
  /addpremium <user_id> [days]   Grant premium (default 30d, 0 = lifetime)
  /removepremium <user_id>       Revoke premium
  /checkpremium <user_id>        Inspect any user's status
  /premiumlist                   List all active premium users

User command:
  /premium                       Check own status + feature breakdown
"""

import time
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

from config import Config
from helper.database import jishubotz


# ── Feature table shown in /premium ───────────────────────────────────────────

_FREE_FEATURES = (
    "Unlimited renames",
    "Custom caption",
    "Prefix / suffix tags",
    "Custom thumbnail",
    "Metadata injection",
    "MediaInfo (/mi)",
    "Screenshot grid  (6 frames)",
    "Sample video     (30s)",
    "Upscale          (3 uses/day)",
)

_PREMIUM_FEATURES = (
    "Everything in Free  +",
    "AI thumbnail upscale  (unlimited)",
    "Screenshot grid  (up to 12 frames)",
    "Sample video     (up to 5 min)",
    "Priority processing",
    "Dump channel support",
    "Early access to new features",
)


def _feature_block(lines: tuple, tick: str = "✅") -> str:
    return "\n".join(f"{tick}  {line}" for line in lines)


# ── /premium ──────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("premium"))
async def cmd_premium(client: Client, message: Message):
    user_id   = message.from_user.id
    user_name = message.from_user.mention

    # ── Admin ─────────────────────────────────────────────────────────────────
    if user_id in Config.ADMIN:
        total_premium = await jishubotz.get_all_premium_users()
        premium_count = 0
        async for _ in total_premium:
            premium_count += 1

        renames = await jishubotz.get_rename_count(user_id)
        return await message.reply_text(
            f"╭━━━〔 👑 TEMPEST COUNCIL 〕━━━╮\n"
            f"┃  🌌  {user_name}\n"
            f"┃  🛡   Role          ·  Admin\n"
            f"┃  ⚡  Access        ·  Lifetime ∞\n"
            f"┃  📊  Total Renames ·  {renames:,}\n"
            f"┃  👑  Premium Users ·  {premium_count:,}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"✨ <b>All Tempest skills unlocked.</b>"
        )

    # ── Check premium status ──────────────────────────────────────────────────
    info       = await jishubotz.get_premium_info(user_id)
    has_prem   = info.get("premium", False) if info else False
    expiry     = info.get("premium_expiry") if info else None
    renames    = await jishubotz.get_rename_count(user_id)
    upscales   = await jishubotz.get_upscale_uses_today(user_id)
    now        = time.time()

    # Auto-expire
    if has_prem and expiry and expiry < now:
        await jishubotz.set_premium(user_id, False)
        has_prem = False
        expiry   = None

    if has_prem:
        # ── Active premium ────────────────────────────────────────────────────
        if expiry:
            dt        = datetime.fromtimestamp(expiry, tz=timezone.utc)
            days_left = max(0, int((expiry - now) / 86400))
            plan_str  = f"{dt.strftime('%d %b %Y')}  ({days_left} day{'s' if days_left != 1 else ''} left)"
        else:
            plan_str = "Lifetime ∞"

        await message.reply_text(
            f"╭━━━〔 👑 TEMPEST ELITE 〕━━━╮\n"
            f"┃  🌌  {user_name}\n"
            f"┃  ⭐  Status        ·  Active\n"
            f"┃  📅  Plan          ·  {plan_str}\n"
            f"┃  📊  Total Renames ·  {renames:,}\n"
            f"┃  🖼️  Upscales Today ·  {upscales}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"<b>✨ Elite Skills:</b>\n"
            f"{_feature_block(_PREMIUM_FEATURES)}\n\n"
            f"<i>Thank you, Tempest traveler ♡</i>"
        )
    else:
        # ── Free user ─────────────────────────────────────────────────────────
        await message.reply_text(
            f"╭━━━〔 👤 TRAVELER STATUS 〕━━━╮\n"
            f"┃  🌌  {user_name}\n"
            f"┃  ❌  Free Plan\n"
            f"┃  📊  Renames     ·  {renames:,}\n"
            f"┃  🖼️  Upscales     ·  {upscales} / 3\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"<b>💠 Free Skills:</b>\n"
            f"{_feature_block(_FREE_FEATURES)}\n\n"
            f"<b>👑 Elite Upgrade:</b>\n"
            f"{_feature_block(_PREMIUM_FEATURES, tick='⚡')}\n\n"
            f"📩 Contact @naruto0927 to evolve."
        )


# ── /addpremium ───────────────────────────────────────────────────────────────

@Client.on_message(filters.command("addpremium") & filters.user(Config.ADMIN))
async def cmd_add_premium(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.reply_text(
            "╭━━━〔 👑 GRANT ELITE 〕━━━╮\n"
            "<b>Usage:</b> <code>/addpremium [user_id] [days]</code>\n\n"
            "<b>Examples:</b>\n"
            "┃  "
            "<code>/addpremium 123456789</code>       ·  30 days\n"
            "<code>/addpremium 123456789 60</code>    ·  60 days\n"
            "<code>/addpremium 123456789 0</code>     ·  Lifetime ∞\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.")

    days = 30
    if len(parts) >= 3:
        try:
            days = int(parts[2])
            if days < 0:
                raise ValueError
        except ValueError:
            return await message.reply_text("❌ Days must be a non-negative integer.")

    now    = time.time()
    expiry = None if days == 0 else now + days * 86400

    # Check existing premium
    existing     = await jishubotz.get_premium_info(target_id)
    was_premium  = (existing or {}).get("premium", False)
    old_expiry   = (existing or {}).get("premium_expiry")

    await jishubotz.set_premium(target_id, True, expiry)

    # Build DM to user
    if expiry:
        dt      = datetime.fromtimestamp(expiry, tz=timezone.utc)
        exp_str = f"{dt.strftime('%d %b %Y')}  ({days} day{'s' if days != 1 else ''})"
    else:
        exp_str = "Lifetime ∞"

    try:
        await client.send_message(
            target_id,
            f"╭━━━〔 👑 ELITE {'RENEWED' if was_premium else 'ACTIVATED'} 〕━━━╮\n"
            f"Plan     →  {'Lifetime ∞' if not days else f'{days} day(s)'}\n"
            f"Expires  →  {exp_str}"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"<b>Premium Features:</b>\n"
            f"{_feature_block(_PREMIUM_FEATURES)}\n\n"
            f"Thank you for supporting the project ♡\n"
            f"Support: @naruto0927"
        )
        dm_status = "✅ DM sent"
    except Exception:
        dm_status = "⚠️ Could not DM user"

    # Build admin confirmation
    old_str = ""
    if was_premium and old_expiry:
        old_dt  = datetime.fromtimestamp(old_expiry, tz=timezone.utc)
        old_str = f"\n┃  📅  Prev Expiry  ·  {old_dt.strftime('%d %b %Y')}"

    await message.reply_text(
        f"╭━━━〔 👑 TEMPEST ELITE GRANTED 〕━━━╮\n"
        f"┃  🆔  User     ·  <code>{target_id}</code>\n"
        f"┃  ⚡  Action   ·  {'Renewed' if was_premium else 'New'}\n"
        f"┃  📅  Plan     ·  {'Lifetime ∞' if not days else f'{days} day(s)'}\n"
        f"┃  🗓   Expires  ·  {exp_str}\n"
        f"┃  📨  DM       ·  {dm_status}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


# ── /removepremium ────────────────────────────────────────────────────────────

@Client.on_message(
    filters.command(["removepremium", "rempremium"]) & filters.user(Config.ADMIN)
)
async def cmd_rem_premium(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.reply_text(
            "╭━━━〔 👑 REVOKE PREMIUM 〕━━━╮\n"
            "┃  Usage: /removepremium [user_id]\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.")

    info       = await jishubotz.get_premium_info(target_id)
    had_prem   = (info or {}).get("premium", False)
    old_expiry = (info or {}).get("premium_expiry")

    await jishubotz.set_premium(target_id, False)

    if not had_prem:
        return await message.reply_text(
            f"╭━━━〔 👑 NOT PREMIUM 〕━━━╮\n"
            f"┃  <code>{target_id}</code> had no active plan.\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    if old_expiry:
        old_dt  = datetime.fromtimestamp(old_expiry, tz=timezone.utc)
        was_str = f"📅  Was expiring  ·  {old_dt.strftime('%d %b %Y')}"
    else:
        was_str = "∞  Was Lifetime plan"

    try:
        await client.send_message(
            target_id,
            "╭━━━〔 👑 TEMPEST STATUS 〕━━━╮\n"
            "┃  ⚠️  Elite access revoked.\n"
            "┃  Contact @naruto0927 to appeal.\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )
        dm_status = "✅ DM sent"
    except Exception:
        dm_status = "⚠️ Could not DM user"

    await message.reply_text(
        f"╭━━━〔 👑 ELITE STATUS REVOKED 〕━━━╮\n"
        f"┃  🆔  <code>{target_id}</code>\n"
        f"┃  📅  {was_str}\n"
        f"┃  📨  DM  ·  {dm_status}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


# ── /checkpremium ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("checkpremium") & filters.user(Config.ADMIN))
async def cmd_check_premium(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.reply_text(
            "╭━━━〔 👑 CHECK ELITE 〕━━━╮\n"
            "┃  Usage: /checkpremium [user_id]\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.")

    now = time.time()

    if target_id in Config.ADMIN:
        renames = await jishubotz.get_rename_count(target_id)
        return await message.reply_text(
            f"╭━━━〔 🛡 COUNCIL MEMBER 〕━━━╮\n"
            f"┃  🆔  <code>{target_id}</code>\n"
            f"┃  🛡   Role     ·  Admin\n"
            f"┃  ⚡  Access  ·  Lifetime ∞\n"
            f"┃  📊  Renames  ·  {renames:,}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    info = await jishubotz.get_premium_info(target_id)
    if not info:
        return await message.reply_text(
            f"╭━━━〔 👑 ELITE CHECK 〕━━━╮\n"
            f"┃  🆔  <code>{target_id}</code>\n"
            f"┃  ⚠️  Not registered.\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    has_prem = info.get("premium", False)
    expiry   = info.get("premium_expiry")
    renames  = int(info.get("total_renames", 0))

    # Auto-expire
    if has_prem and expiry and expiry < now:
        await jishubotz.set_premium(target_id, False)
        has_prem = False

    if not has_prem:
        return await message.reply_text(
            f"╭━━━〔 👑 ELITE CHECK 〕━━━╮\n"
            f"┃  🆔  <code>{target_id}</code>\n"
            f"┃  ❌  Free Plan\n"
            f"┃  📊  Renames  ·  {renames:,}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    if expiry:
        dt        = datetime.fromtimestamp(expiry, tz=timezone.utc)
        days_left = max(0, int((expiry - now) / 86400))
        exp_line  = f"{dt.strftime('%d %b %Y')}  ({days_left}d left)"
    else:
        exp_line = "Lifetime ∞"

    await message.reply_text(
        f"╭━━━〔 👑 ELITE CHECK 〕━━━╮\n"
        f"┃  🆔  <code>{target_id}</code>\n"
        f"┃  ⭐  Status    ·  Active\n"
        f"┃  📅  Expires   ·  {exp_line}\n"
        f"┃  📊  Renames   ·  {renames:,}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


# ── /premiumlist ──────────────────────────────────────────────────────────────

@Client.on_message(filters.command("premiumlist") & filters.user(Config.ADMIN))
async def cmd_premium_list(client: Client, message: Message):
    users    = await jishubotz.get_all_premium_users()
    now      = time.time()
    lines    = ["╭━━━〔 👑 ELITE MEMBERS 〕━━━╮"]
    count    = 0
    expired  = []
    lifetime = 0

    async for user in users:
        uid    = user["_id"]
        expiry = user.get("premium_expiry")
        if expiry and expiry < now:
            expired.append(uid)
            continue
        renames = int(user.get("total_renames", 0))
        if expiry:
            dt        = datetime.fromtimestamp(expiry, tz=timezone.utc)
            days_left = max(0, int((expiry - now) / 86400))
            lines.append(
                f"┃  👑  <code>{uid}</code>\n"
                f"┃     📅 Expires  ·  {dt.strftime('%d %b %Y')} ({days_left}d)\n"
                f"┃     📊 Renames  ·  {renames:,}"
            )
        else:
            lifetime += 1
            lines.append(
                f"┃  ∞  <code>{uid}</code>\n"
                f"┃     📅 Plan     ·  Lifetime ∞\n"
                f"┃     📊 Renames  ·  {renames:,}"
            )
        count += 1

    for uid in expired:
        await jishubotz.set_premium(uid, False)

    if count == 0:
        lines.append("┃  ⚠️  No Elite members yet.\n╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
    else:
        lines.append("╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        lines.append(
            f"<blockquote>Active · {count}  ·  Lifetime · {lifetime}  ·  Expired · {len(expired)}</blockquote>"
        )

    await message.reply_text("\n".join(lines))
