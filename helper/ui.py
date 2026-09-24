"""
helper/ui.py  ─  Rimuru Tempest UI theme engine
══════════════════════════════════════════════════════════════════════════════
Central design system for all bot messages, keyboards, and progress displays.
Import this module everywhere instead of hand-crafting messages.

Usage:
    from helper.ui import RUI, KB

    text   = RUI.card("FILE ACQUIRED", [("Name", "movie.mkv"), ("Size", "1.4 GB")])
    markup = KB.row([("⚡ Rename", "upload_document"), ("🌌 Cancel", "cancel")])
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import random
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# ──────────────────────────────────────────────────────────────────────────────
# Rimuru System Messages  (flavour text shown contextually)
# ──────────────────────────────────────────────────────────────────────────────

_SAGE_LINES = [
    "⚡ Great Sage analyzing request...",
    "🧬 Predator skill activated...",
    "🌌 Magicule flow stabilizing...",
    "💠 Tempest core initializing...",
    "🔮 Evolution sequence engaged...",
    "✨ Rimuru System processing...",
    "🛡 Barrier erected. Standing by...",
    "🌀 Spatial compression active...",
]

_DONE_LINES = [
    "✨ Evolution Complete",
    "💠 Skill Mastered",
    "🌌 Assimilation Successful",
    "⚡ Tempest Power Applied",
    "🔮 Predation Complete",
    "✅ Great Sage Confirms: Done",
]

_ERROR_LINES = [
    "❌ Skill Activation Failed",
    "⚠️ Magicule Disruption Detected",
    "🛡 Barrier Breach — Error",
    "❌ Great Sage: Anomaly Found",
]


def sage_line() -> str:
    return random.choice(_SAGE_LINES)

def done_line() -> str:
    return random.choice(_DONE_LINES)

def error_line() -> str:
    return random.choice(_ERROR_LINES)


# ──────────────────────────────────────────────────────────────────────────────
# Core UI builder  — RUI
# ──────────────────────────────────────────────────────────────────────────────

class RUI:
    """Rimuru UI factory. All methods return formatted HTML strings."""

    # ── Borders ───────────────────────────────────────────────────────────────

    TOP    = "╭━━━〔 {} 〕━━━╮"
    MID    = "┃  {}"
    SEP    = "┣━━━━━━━━━━━━━━━━━━━━━━━━━"
    BOT    = "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    RULER  = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"

    # ── Card builder ──────────────────────────────────────────────────────────

    @staticmethod
    def card(
        title: str,
        rows: list[tuple[str, str]] | None = None,
        footer: str | None = None,
        icon: str = "💠",
    ) -> str:
        """
        Build a bordered card.

        card("FILE ACQUIRED", [("Name", "movie.mkv"), ("Size", "1.4 GB")])
        →
        ╭━━━〔 💠 FILE ACQUIRED 〕━━━╮
        ┃  📛 Name  ·  movie.mkv
        ┃  📦 Size  ·  1.4 GB
        ╰━━━━━━━━━━━━━━━━━━━━━━━━╯
        """
        lines = [f"╭━━━〔 {icon} <b>{title}</b> {icon} 〕━━━╮"]
        if rows:
            for label, value in rows:
                lines.append(f"┃  {label}  ·  {value}")
        if footer:
            lines.append("┣━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"┃  {footer}")
        lines.append("╰━━━━━━━━━━━━━━━━━━━━━━━━╯")
        return "\n".join(lines)

    @staticmethod
    def header(title: str, subtitle: str = "", icon: str = "💠") -> str:
        """Single header line with optional subtitle."""
        h = f"{icon} <b>{title}</b>"
        if subtitle:
            h += f"\n<i>{subtitle}</i>"
        return h

    @staticmethod
    def field(label: str, value: str, bullet: str = "┃") -> str:
        return f"{bullet}  <b>{label}</b>  ·  {value}"

    @staticmethod
    def info(text: str) -> str:
        """Soft info box."""
        return f"<blockquote>💠  {text}</blockquote>"

    @staticmethod
    def warn(text: str) -> str:
        return f"<blockquote>⚠️  {text}</blockquote>"

    @staticmethod
    def error(text: str) -> str:
        return f"<blockquote>❌  {text}</blockquote>"

    @staticmethod
    def success(text: str) -> str:
        return f"<blockquote>✨  {text}</blockquote>"

    @staticmethod
    def ruler() -> str:
        return "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"

    # ── Progress display ──────────────────────────────────────────────────────

    @staticmethod
    def progress_bar(pct: int, width: int = 10) -> str:
        filled = int(pct / 100 * width)
        empty  = width - filled
        return f"[{'█' * filled}{'░' * empty}] {pct}%"

    @staticmethod
    def progress_card(
        job_id: str,
        filename: str,
        phase: str,
        pct: int,
        speed: float,
        humanbytes_fn,
    ) -> str:
        phase_icons = {
            "downloading": "⬇️  Acquiring",
            "processing":  "⚙️  Evolving",
            "uploading":   "⬆️  Transmitting",
            "done":        "✨  Complete",
        }
        label    = phase_icons.get(phase, f"⚡  {phase.title()}")
        bar      = RUI.progress_bar(pct)
        spd_str  = f"  ·  {humanbytes_fn(speed)}/s" if speed > 0 else ""
        name_str = filename if len(filename) <= 46 else filename[:43] + "…"

        lines = [
            f"╭━━━〔 💠 RIMURU SYSTEM 〕━━━╮",
            f"┃  🆔  <code>{job_id}</code>",
            f"┃  📂  <code>{name_str}</code>",
            f"┣━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"┃  {label}",
            f"┃  <code>{bar}</code>{spd_str}",
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯",
        ]
        return "\n".join(lines)

    # ── File info card ────────────────────────────────────────────────────────

    @staticmethod
    def file_card(
        filename: str,
        size: str = "",
        status: str = "",
        extra: list[tuple[str, str]] | None = None,
    ) -> str:
        name_str = filename if len(filename) <= 44 else filename[:41] + "…"
        rows: list[tuple[str, str]] = [("📂  File", f"<code>{name_str}</code>")]
        if size:
            rows.append(("📦  Size", size))
        if status:
            rows.append(("⚡  Status", status))
        if extra:
            rows.extend(extra)
        return RUI.card("FILE SYSTEM", rows)

    # ── Premium badge ─────────────────────────────────────────────────────────

    @staticmethod
    def premium_badge(lifetime: bool = False) -> str:
        tier = "∞ Tempest Infinity" if lifetime else "👑 Tempest Elite"
        return f"<b>{tier}</b>"

    # ── List builder ──────────────────────────────────────────────────────────

    @staticmethod
    def checklist(items: list[str], tick: str = "✨") -> str:
        return "\n".join(f"{tick}  {item}" for item in items)

    @staticmethod
    def dotlist(items: list[str]) -> str:
        return "\n".join(f"┃  💠  {item}" for item in items)


# ──────────────────────────────────────────────────────────────────────────────
# Keyboard builder  — KB
# ──────────────────────────────────────────────────────────────────────────────

class KB:
    """Keyboard factory for consistent button layouts."""

    @staticmethod
    def btn(label: str, callback: str | None = None, url: str | None = None) -> InlineKeyboardButton:
        if url:
            return InlineKeyboardButton(label, url=url)
        return InlineKeyboardButton(label, callback_data=callback or "noop")

    @staticmethod
    def row(buttons: list[tuple[str, str] | tuple[str, str, str]]) -> list[InlineKeyboardButton]:
        """
        buttons = [(label, callback), ...] or [(label, callback, "url"), ...]
        """
        row = []
        for item in buttons:
            if len(item) == 3 and item[2] == "url":
                row.append(InlineKeyboardButton(item[0], url=item[1]))
            else:
                row.append(InlineKeyboardButton(item[0], callback_data=item[1]))
        return row

    @staticmethod
    def grid(
        buttons: list[tuple[str, str]],
        cols: int = 2,
        *,
        back: str | None = None,
        close: bool = True,
    ) -> InlineKeyboardMarkup:
        """Auto-grid buttons into `cols` columns with optional back/close row."""
        rows = []
        for i in range(0, len(buttons), cols):
            rows.append([InlineKeyboardButton(b[0], callback_data=b[1]) for b in buttons[i:i+cols]])
        nav = []
        if back:
            nav.append(InlineKeyboardButton("↩ Return", callback_data=back))
        if close:
            nav.append(InlineKeyboardButton("✕ Dismiss", callback_data="close"))
        if nav:
            rows.append(nav)
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def back(to: str = "start", label: str = "↩ Return") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(label, callback_data=to),
            InlineKeyboardButton("✕ Dismiss", callback_data="close"),
        ]])

    @staticmethod
    def confirm(yes_cb: str, no_cb: str = "close") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=yes_cb),
            InlineKeyboardButton("✕ Cancel",  callback_data=no_cb),
        ]])

    @staticmethod
    def toggle(label: str, state: bool, on_cb: str, off_cb: str) -> InlineKeyboardButton:
        icon = "🟢" if state else "🔴"
        return InlineKeyboardButton(f"{icon}  {label}", callback_data=off_cb if state else on_cb)
