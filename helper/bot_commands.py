"""
helper/bot_commands.py
══════════════════════════════════════════════════════════════════════════════
Automatic Telegram command menu registration.

Source of truth
───────────────
• USER_CMDS  in plugins/help_menu.py  — user-facing command groups + descriptions
• ADMIN_CMDS in plugins/help_menu.py  — admin command groups + descriptions
• Decorator scan below                — actual command names extracted at import

How it works
────────────
  1. At startup, read USER_CMDS and ADMIN_CMDS from help_menu for descriptions.
  2. Map each group key → one or more actual Telegram command names using the
     COMMAND_MAP table defined here (single place to maintain).
  3. Build BotCommand lists for each Telegram scope.
  4. Call set_bot_commands() once — Pyrogram's wrapper for setMyCommands.
  5. Skip the API call if the command list hasn't changed (hash comparison).

Adding a new command
────────────────────
  1. Add the @Client.on_message(filters.command("xyz")) decorator in a plugin.
  2. If it needs a help description, add it to USER_CMDS or ADMIN_CMDS in
     help_menu.py.
  3. Add the key → ["xyz"] entry to COMMAND_MAP below.
  4. Restart the bot — Telegram's menu updates automatically.

Scopes used
───────────
  • BotCommandScopeAllPrivateChats  → user commands (visible to everyone)
  • BotCommandScopeAllGroupChats    → (empty — bot is private-only)
  • BotCommandScopeChat (per admin) → user + admin commands for each admin

The admin scope is per-chat (personal) so admin commands never appear in other
users' menus.
══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrogram import Client

log = logging.getLogger(__name__)

# ── Mapping: help_menu key → actual Telegram command name(s) ─────────────────
#
# Keys must match the keys in USER_CMDS / ADMIN_CMDS in help_menu.py.
# Values are the real command names used in @Client.on_message(filters.command(...)).
# List multiple names when a command has aliases (only the first is shown in
# Telegram's menu; the rest still work as aliases).
#
# USER commands
USER_COMMAND_MAP: dict[str, list[str]] = {
    "mode":        ["mode"],
    "rename":      [],                         # triggered by file message, not /rename
    "autorename":  ["autorename"],
    "setsource":   ["setsource"],
    "setmedia":    ["setmedia"],
    "autoqueue":   ["autoqueue"],
    "thumbnail":   ["view_thumb", "del_thumb"],
    "caption":     ["set_caption", "see_caption", "del_caption"],
    "prefix":      ["set_prefix", "see_prefix", "del_prefix",
                    "set_suffix", "see_suffix", "del_suffix"],
    "metadata":    ["metadata"],
    "mediainfo":   ["mi"],
    "screenshot":  [],                         # button on file, no slash command
    "sample":      [],                         # button on file, no slash command
    "dump":        ["dump"],
    "history":     ["history", "h"],
    "leaderboard": ["leaderboard"],
    "premium":     ["premium"],
}

# ADMIN commands
ADMIN_COMMAND_MAP: dict[str, list[str]] = {
    "limits":       ["limit", "setlimit", "getlimit"],
    "jobs":         ["jobs"],
    "broadcast":    ["broadcast"],
    "ban":          ["ban", "unban"],
    "premium_adm":  ["addpremium", "removepremium", "checkpremium", "premiumlist"],
    "queue_admin":  ["clearqueue"],
    "msettings":    ["media_settings", "set_sample", "set_ss", "set_upscale"],
    "panel":        ["panel"],
    "restart":      ["restart"],
}

# Commands shown to all users (in addition to the above user commands)
ALWAYS_VISIBLE: list[tuple[str, str]] = [
    ("start",  "Start the bot"),
    ("help",   "Open help menu"),
    ("ping",   "Check bot status"),
    ("status", "Your rename status"),
]

# ── Cached hash of last-registered commands ──────────────────────────────────
_last_user_hash:  str = ""
_last_admin_hash: str = ""


def _descriptions_from_help() -> tuple[dict[str, str], dict[str, str]]:
    """
    Import USER_CMDS and ADMIN_CMDS from help_menu and extract short labels.
    Returns (user_desc, admin_desc) mapping group_key → short one-line label.
    Avoids circular import by importing inside the function (help_menu does not
    import bot_commands).
    """
    from plugins.help_menu import USER_CMDS, ADMIN_CMDS   # noqa: PLC0415
    user_desc  = {k: v[0] for k, v in USER_CMDS.items()}
    admin_desc = {k: v[0] for k, v in ADMIN_CMDS.items()}
    return user_desc, admin_desc


def _build_user_commands() -> list[tuple[str, str]]:
    """
    Build (command, description) pairs for the user scope.

    • ALWAYS_VISIBLE commands first
    • Then one entry per command in USER_COMMAND_MAP that has ≥1 real name
    • Description comes from USER_CMDS[key][0] (the short label)
    • Telegram enforces: name ≤32 chars, description ≤256 chars, max 100 cmds
    """
    user_desc, _ = _descriptions_from_help()
    cmds: list[tuple[str, str]] = list(ALWAYS_VISIBLE)
    seen: set[str] = {c for c, _ in cmds}

    for key, names in USER_COMMAND_MAP.items():
        for name in names:
            if name in seen:
                continue
            desc  = user_desc.get(key, _fallback_desc(name))
            # Strip emoji from description so it fits cleanly in the menu
            desc  = _strip_html(desc)[:256]
            cmds.append((name, desc))
            seen.add(name)
            break   # only the first name per group goes in the menu

    return cmds


def _build_admin_commands(user_cmds: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Build (command, description) pairs for the admin scope.
    Includes all user commands plus admin-only commands.
    """
    _, admin_desc = _descriptions_from_help()
    cmds: list[tuple[str, str]] = list(user_cmds)
    seen: set[str] = {c for c, _ in cmds}

    # Admin stats command (not in user scope)
    if "stats" not in seen:
        cmds.append(("stats", "Bot performance stats"))
        seen.add("stats")

    for key, names in ADMIN_COMMAND_MAP.items():
        for name in names:
            if name in seen:
                continue
            desc = admin_desc.get(key, _fallback_desc(name))
            desc = _strip_html(desc)[:256]
            cmds.append((name, desc))
            seen.add(name)
            break   # first name only in menu

    return cmds


def _fallback_desc(cmd_name: str) -> str:
    """Generate a readable description from a command name."""
    return cmd_name.replace("_", " ").capitalize()


def _strip_html(text: str) -> str:
    """Remove HTML tags and leading ◈/emoji for clean menu descriptions."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"◈\s*", "", text)
    text = text.strip().split("\n")[0].strip()
    return text


def _hash_cmds(cmds: list[tuple[str, str]]) -> str:
    payload = json.dumps(sorted(cmds), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


async def update_bot_commands(client: "Client", force: bool = False) -> None:
    """
    Register the bot's command menu with Telegram.

    • user scope  — BotCommandScopeAllPrivateChats (visible to every user)
    • admin scope — BotCommandScopeChat per admin  (their personal menu)

    Skips the API calls if the command list has not changed since last run
    (unless force=True).  Safe to call multiple times; idempotent.

    Does NOT crash on Telegram API errors — logs and continues.
    """
    global _last_user_hash, _last_admin_hash

    from pyrogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
    from config import Config

    try:
        user_pairs  = _build_user_commands()
        admin_pairs = _build_admin_commands(user_pairs)
    except Exception as e:
        log.error("[bot_commands] Failed to build command list: %s", e)
        return

    user_hash  = _hash_cmds(user_pairs)
    admin_hash = _hash_cmds(admin_pairs)

    user_changed  = force or user_hash  != _last_user_hash
    admin_changed = force or admin_hash != _last_admin_hash

    # ── User scope ─────────────────────────────────────────────────────────
    if user_changed:
        user_bc = [BotCommand(cmd, desc) for cmd, desc in user_pairs]
        try:
            await client.set_bot_commands(
                user_bc,
                scope=BotCommandScopeAllPrivateChats(),
            )
            _last_user_hash = user_hash
            log.info(
                "[bot_commands] User menu registered: %d commands  [hash=%s]",
                len(user_bc), user_hash,
            )
        except Exception as e:
            log.error("[bot_commands] set_bot_commands (user scope) failed: %s", e)
    else:
        log.info("[bot_commands] User menu unchanged — skipping API call")

    # ── Admin scope (per admin chat) ───────────────────────────────────────
    if admin_changed:
        admin_bc = [BotCommand(cmd, desc) for cmd, desc in admin_pairs]
        for admin_id in Config.ADMIN:
            try:
                await client.set_bot_commands(
                    admin_bc,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
                log.info(
                    "[bot_commands] Admin menu registered for %s: %d commands  [hash=%s]",
                    admin_id, len(admin_bc), admin_hash,
                )
            except Exception as e:
                # Common reason: admin has never started the bot
                log.warning(
                    "[bot_commands] Admin scope for %s failed (user may not have started bot): %s",
                    admin_id, e,
                )
        _last_admin_hash = admin_hash
    else:
        log.info("[bot_commands] Admin menu unchanged — skipping API call")
