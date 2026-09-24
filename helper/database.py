"""
helper/database.py
══════════════════════════════════════════════════════════════════════════════
Optimisation principles:
  1. get_pipeline_settings() — ONE round-trip per rename; callers never call
     get_prefix/get_suffix/get_caption/… individually in hot paths.
  2. Every find_one uses a projection — only requested fields travel the wire.
  3. All writes use upsert=True — no pre-flight existence check needed.
  4. Read-then-write ops (inc_auto_rename_today, inc_rename_count) use
     find_one_and_update — single atomic round-trip.
  5. get_limits() reads the full bot_settings doc once and returns all limits.
  6. _get/_set helpers keep individual setters/getters DRY without overhead.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import time
import datetime

import motor.motor_asyncio
from config import Config
from .utils import send_log
from messages import log, Msg

# Defaults (single source of truth)
_D_GLOBAL_TX    = 10
_D_USER_TX      = 3
_D_AUTO_DAILY   = 30
_D_TRANSMISSION  = 4
_D_MANUAL_DAILY  = 10

_META_DEFAULTS = {
    "title": "", "author": "", "artist": "",
    "audio": "", "video": "", "subtitle": "", "comment": "",
}

# Projection for full pipeline load
_PIPELINE_PROJ = {
    "file_id": 1, "caption": 1, "prefix": 1, "suffix": 1,
    "metadata": 1, "metadata_fields": 1,
    "dump_channel": 1, "dump_mode": 1,
    "sample_duration": 1, "screenshot_count": 1, "upscale_factor": 1,
    "rename_mode": 1, "format_template": 1, "rename_source": 1,
    "auto_media_type": 1, "auto_rename_daily": 1,
    "manual_rename_daily": 1,
    "premium": 1, "premium_expiry": 1,
}

# ── In-memory bot_settings cache ──────────────────────────────────────────────
# bot_settings (global limits, concurrency) change only when admin uses /setlimit.
# Caching them avoids a DB round-trip on every rename job.
# TTL: 60 seconds — stale-reads are harmless (limits are approximate by nature).
_bot_settings_cache: dict = {}
_bot_settings_ts: float = 0.0
_BOT_SETTINGS_TTL = 60.0   # seconds


class Database:

    def __init__(self, uri: str, database_name: str):
        self._client    = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.jishubotz  = self._client[database_name]
        self.col        = self.jishubotz.user
        self.bannedList = self.jishubotz.bannedList

    # ─────────────────────── schema ─────────────────────────────────────────

    def new_user(self, id: int) -> dict:
        return dict(
            _id=int(id),
            file_id=None, caption=None, prefix=None, suffix=None,
            metadata=False, metadata_code="@Animes_Ocean",
            metadata_fields=dict(_META_DEFAULTS),
            dump_channel=None, sample_video=False,
            screenshot=False, dump_mode=False,
            sample_duration=30, screenshot_count=6, upscale_factor=2,
            rename_mode="manual", format_template=None,
            rename_source="filename", auto_media_type=None,
            premium=False, premium_expiry=None,
        )

    # ─────────────────────── core helpers ───────────────────────────────────

    async def _get(self, user_id: int, field: str, default=None):
        """Single-field getter with projection."""
        doc = await self.col.find_one({"_id": int(user_id)}, {field: 1})
        return (doc or {}).get(field, default)

    async def _set(self, user_id: int, field: str, value) -> None:
        """Single-field upsert-safe setter."""
        await self.col.update_one(
            {"_id": int(user_id)}, {"$set": {field: value}}, upsert=True,
        )

    # ─────────────────────── pipeline bulk-load ──────────────────────────────

    async def get_pipeline_settings(self, user_id: int) -> dict:
        """
        Return ALL settings needed by the rename pipeline in ONE round-trip.

        Use this in _pipeline and _run_pipeline instead of calling
        get_prefix / get_suffix / get_caption / get_metadata … individually.

        Returned dict keys:
          thumbnail, caption, prefix, suffix,
          metadata (bool), metadata_fields (dict),
          dump_channel, dump_mode (bool),
          sample_duration, screenshot_count, upscale_factor,
          rename_mode, format_template, rename_source, auto_media_type,
          premium (bool), is_admin (bool), auto_daily_count (int today)
        """
        doc = await self.col.find_one({"_id": int(user_id)}, _PIPELINE_PROJ) or {}

        is_admin   = int(user_id) in Config.ADMIN
        is_premium = is_admin
        if not is_premium and doc.get("premium"):
            expiry = doc.get("premium_expiry")
            is_premium = (expiry is None or expiry >= time.time())

        today             = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        daily_count       = int((doc.get("auto_rename_daily") or {}).get(today, 0))
        manual_day_count  = int((doc.get("manual_rename_daily") or {}).get(today, 0))

        raw_fields  = doc.get("metadata_fields") or {}
        return {
            "thumbnail":        doc.get("file_id"),
            "caption":          doc.get("caption"),
            "prefix":           doc.get("prefix") or "",
            "suffix":           doc.get("suffix") or "",
            "metadata":         bool(doc.get("metadata", False)),
            "metadata_fields":  {**_META_DEFAULTS, **raw_fields},
            "dump_channel":     doc.get("dump_channel"),
            "dump_mode":        bool(doc.get("dump_mode", False)),
            "sample_duration":  int(doc.get("sample_duration",   30)),
            "screenshot_count": int(doc.get("screenshot_count",  6)),
            "upscale_factor":   int(doc.get("upscale_factor",    2)),
            "rename_mode":      doc.get("rename_mode", "manual"),
            "format_template":  doc.get("format_template"),
            "rename_source":    (await self._get_bot_settings()).get("global_rename_source", "filename"),
            "auto_media_type":  doc.get("auto_media_type"),
            "premium":            is_premium,
            "is_admin":           is_admin,
            "auto_daily_count":   daily_count,
            "manual_daily_count": manual_day_count,
        }

    # ─────────────────────── user management ────────────────────────────────

    async def add_user(self, b, m) -> None:
        u = m.from_user
        result = await self.col.update_one(
            {"_id": int(u.id)},
            {"$setOnInsert": self.new_user(u.id)},
            upsert=True,
        )
        if result.upserted_id is not None:
            # New user — send log
            await send_log(b, u)

    async def is_user_exist(self, user_id: int) -> bool:
        return bool(await self.col.find_one({"_id": int(user_id)}, {"_id": 1}))

    async def total_users_count(self) -> int:
        return await self.col.count_documents({})

    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id: int) -> None:
        await self.col.delete_many({"_id": int(user_id)})

    # ─────────────────────── thumbnail ──────────────────────────────────────

    async def set_thumbnail(self, user_id: int, file_id) -> None:
        await self._set(user_id, "file_id", file_id)

    async def get_thumbnail(self, user_id: int):
        return await self._get(user_id, "file_id")

    # ─────────────────────── caption ────────────────────────────────────────

    async def set_caption(self, user_id: int, caption) -> None:
        await self._set(user_id, "caption", caption)

    async def get_caption(self, user_id: int):
        return await self._get(user_id, "caption")

    # ─────────────────────── prefix / suffix ────────────────────────────────

    async def set_prefix(self, user_id: int, value) -> None:
        await self._set(user_id, "prefix", value)

    async def get_prefix(self, user_id: int):
        return await self._get(user_id, "prefix")

    async def set_suffix(self, user_id: int, value) -> None:
        await self._set(user_id, "suffix", value)

    async def get_suffix(self, user_id: int):
        return await self._get(user_id, "suffix")

    # ─────────────────────── metadata toggle ────────────────────────────────

    async def set_metadata(self, user_id: int, value: bool) -> None:
        await self.col.update_one(
            {"_id": int(user_id)}, {"$set": {"metadata": bool(value)}}, upsert=True,
        )

    async def get_metadata(self, user_id: int) -> bool:
        return bool(await self._get(user_id, "metadata", False))

    async def set_metadata_code(self, user_id: int, code: str) -> None:
        await self._set(user_id, "metadata_code", code)

    async def get_metadata_code(self, user_id: int) -> str:
        return await self._get(user_id, "metadata_code", "")

    # ─────────────────────── per-field metadata ─────────────────────────────

    async def set_metadata_field(self, user_id: int, field: str, value: str) -> None:
        await self.col.update_one(
            {"_id": int(user_id)},
            {"$set": {f"metadata_fields.{field}": value}},
            upsert=True,
        )

    async def get_metadata_fields(self, user_id: int) -> dict:
        doc = await self.col.find_one({"_id": int(user_id)}, {"metadata_fields": 1}) or {}
        return {**_META_DEFAULTS, **(doc.get("metadata_fields") or {})}

    async def get_metadata_field(self, user_id: int, field: str) -> str:
        fields = await self.get_metadata_fields(user_id)
        return fields.get(field, "")

    # ─────────────────────── ban management ─────────────────────────────────

    async def ban_user(self, user_id: int) -> bool:
        if await self.bannedList.find_one({"banId": int(user_id)}, {"_id": 1}):
            return False
        await self.bannedList.insert_one({"banId": int(user_id)})
        return True

    async def is_banned(self, user_id: int) -> bool:
        return bool(await self.bannedList.find_one({"banId": int(user_id)}, {"_id": 1}))

    async def is_unbanned(self, user_id: int) -> bool:
        try:
            r = await self.bannedList.delete_one({"banId": int(user_id)})
            return r.deleted_count > 0
        except Exception as e:
            log.error(Msg.DB_UNBAN_ERR, user_id=user_id, error=e)
            return False

    # ─────────────────────── user settings ──────────────────────────────────

    async def set_dump_channel(self, user_id: int, channel_id: int | None) -> None:
        await self._set(user_id, "dump_channel", channel_id)

    async def get_dump_channel(self, user_id: int) -> int | None:
        return await self._get(user_id, "dump_channel")

    async def set_sample_video(self, user_id: int, value: bool) -> None:
        await self._set(user_id, "sample_video", bool(value))

    async def get_sample_video(self, user_id: int) -> bool:
        return bool(await self._get(user_id, "sample_video", False))

    async def set_screenshot(self, user_id: int, value: bool) -> None:
        await self._set(user_id, "screenshot", bool(value))

    async def get_screenshot(self, user_id: int) -> bool:
        return bool(await self._get(user_id, "screenshot", False))

    async def set_dump_mode(self, user_id: int, value: bool) -> None:
        await self._set(user_id, "dump_mode", bool(value))

    async def get_dump_mode(self, user_id: int) -> bool:
        return bool(await self._get(user_id, "dump_mode", False))

    async def set_sample_duration(self, user_id: int, value: int) -> None:
        await self._set(user_id, "sample_duration", int(value))

    async def get_sample_duration(self, user_id: int) -> int:
        return int(await self._get(user_id, "sample_duration", 30))

    async def set_screenshot_count(self, user_id: int, value: int) -> None:
        await self._set(user_id, "screenshot_count", int(value))

    async def get_screenshot_count(self, user_id: int) -> int:
        return int(await self._get(user_id, "screenshot_count", 6))

    async def set_upscale_factor(self, user_id: int, value: int) -> None:
        await self._set(user_id, "upscale_factor", int(value))

    async def get_upscale_factor(self, user_id: int) -> int:
        return int(await self._get(user_id, "upscale_factor", 2))

    async def get_user_settings(self, user_id: int) -> dict:
        """Bulk settings read — single round-trip."""
        proj = {
            "dump_channel": 1, "sample_video": 1, "screenshot": 1,
            "dump_mode": 1, "sample_duration": 1,
            "screenshot_count": 1, "upscale_factor": 1,
        }
        doc = await self.col.find_one({"_id": int(user_id)}, proj) or {}
        return {
            "dump_channel":     doc.get("dump_channel"),
            "sample_video":     bool(doc.get("sample_video",     False)),
            "screenshot":       bool(doc.get("screenshot",       False)),
            "dump_mode":        bool(doc.get("dump_mode",        False)),
            "sample_duration":  int(doc.get("sample_duration",   30)),
            "screenshot_count": int(doc.get("screenshot_count",  6)),
            "upscale_factor":   int(doc.get("upscale_factor",    2)),
        }

    # ─────────────────────── upscale daily usage ─────────────────────────────

    async def get_upscale_uses_today(self, user_id: int) -> int:
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        doc   = await self.col.find_one({"_id": int(user_id)}, {"upscale_uses": 1}) or {}
        rec   = doc.get("upscale_uses") or {}
        return int(rec.get("count", 0)) if isinstance(rec, dict) and rec.get("date") == today else 0

    async def inc_upscale_uses(self, user_id: int) -> int:
        today   = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        doc     = await self.col.find_one({"_id": int(user_id)}, {"upscale_uses": 1}) or {}
        rec     = doc.get("upscale_uses") or {}
        count   = (int(rec.get("count", 0)) + 1) if isinstance(rec, dict) and rec.get("date") == today else 1
        await self.col.update_one(
            {"_id": int(user_id)},
            {"$set": {"upscale_uses": {"date": today, "count": count}}},
            upsert=True,
        )
        return count

    # ─────────────────────── premium ────────────────────────────────────────

    async def set_premium(self, user_id: int, value: bool, expiry=None) -> None:
        await self.col.update_one(
            {"_id": int(user_id)},
            {"$set": {"premium": bool(value), "premium_expiry": expiry}},
            upsert=True,
        )

    async def is_premium(self, user_id: int) -> bool:
        if int(user_id) in Config.ADMIN:
            return True
        doc = await self.col.find_one(
            {"_id": int(user_id)}, {"premium": 1, "premium_expiry": 1}
        ) or {}
        if not doc.get("premium"):
            return False
        expiry = doc.get("premium_expiry")
        if expiry is not None and expiry < time.time():
            await self.set_premium(int(user_id), False)
            return False
        return True

    async def get_premium_info(self, user_id: int):
        return await self.col.find_one({"_id": int(user_id)})

    async def get_all_premium_users(self):
        return self.col.find({"premium": True})

    # ─────────────────────── panel images ───────────────────────────────────

    async def get_panel_images(self) -> dict:
        return await self.jishubotz.panel.find_one({"_id": 0}) or {}

    async def set_panel_image(self, key: str, file_id: str) -> None:
        await self.jishubotz.panel.update_one(
            {"_id": 0}, {"$set": {key: file_id}}, upsert=True,
        )

    async def del_panel_image(self, key: str) -> None:
        await self.jishubotz.panel.update_one(
            {"_id": 0}, {"$unset": {key: ""}}, upsert=True,
        )

    async def get_pic(self, key: str, fallback: str | None = None) -> str | None:
        doc = await self.jishubotz.panel.find_one({"_id": 0}, {key: 1}) or {}
        return doc.get(key) or fallback

    async def _get_bot_settings(self) -> dict:
        """
        Return the full bot_settings doc, cached for _BOT_SETTINGS_TTL seconds.
        Avoids a DB round-trip on every pipeline job for limits that rarely change.
        Cache is invalidated by set_bot_setting() and set_limits().
        """
        global _bot_settings_cache, _bot_settings_ts
        now = time.monotonic()
        if _bot_settings_cache and (now - _bot_settings_ts) < _BOT_SETTINGS_TTL:
            return _bot_settings_cache
        doc = await self.jishubotz.bot_settings.find_one({"_id": 0}) or {}
        _bot_settings_cache = doc
        _bot_settings_ts    = now
        return doc

    def _invalidate_bot_settings_cache(self) -> None:
        global _bot_settings_cache, _bot_settings_ts
        _bot_settings_cache = {}
        _bot_settings_ts    = 0.0

    async def get_daily_limits_cached(self) -> tuple[int, int]:
        """
        Return (auto_daily_limit, manual_daily_limit) from cached bot_settings.
        Zero extra DB round-trips when called alongside get_pipeline_settings.
        """
        doc = await self._get_bot_settings()
        return (
            int(doc.get("auto_daily_limit",   _D_AUTO_DAILY)),
            int(doc.get("manual_daily_limit",  _D_MANUAL_DAILY)),
        )

    # ─────────────────────── bot-level settings ──────────────────────────────

    async def get_bot_setting(self, key: str, default=None):
        doc = await self.jishubotz.bot_settings.find_one({"_id": 0}, {key: 1}) or {}
        return doc.get(key, default)

    async def set_bot_setting(self, key: str, value) -> None:
        await self.jishubotz.bot_settings.update_one(
            {"_id": 0}, {"$set": {key: value}}, upsert=True,
        )
        self._invalidate_bot_settings_cache()

    async def get_limits(self) -> tuple[int, int, int, int, int]:
        """Single round-trip: (global, user, auto_daily, transmission, manual_daily)."""
        doc = await self.jishubotz.bot_settings.find_one({"_id": 0}) or {}
        return (
            int(doc.get("global_limit",       _D_GLOBAL_TX)),
            int(doc.get("user_limit",          _D_USER_TX)),
            int(doc.get("auto_daily_limit",    _D_AUTO_DAILY)),
            int(doc.get("transmission_limit",  _D_TRANSMISSION)),
            int(doc.get("manual_daily_limit",  _D_MANUAL_DAILY)),
        )

    async def set_limits(
        self,
        global_limit:       int | None = None,
        user_limit:         int | None = None,
        auto_daily_limit:   int | None = None,
        transmission_limit: int | None = None,
        manual_daily_limit: int | None = None,
    ) -> None:
        update = {}
        if global_limit       is not None: update["global_limit"]       = int(global_limit)
        if user_limit         is not None: update["user_limit"]         = int(user_limit)
        if auto_daily_limit   is not None: update["auto_daily_limit"]   = int(auto_daily_limit)
        if transmission_limit is not None: update["transmission_limit"] = int(transmission_limit)
        if manual_daily_limit is not None: update["manual_daily_limit"] = int(manual_daily_limit)
        if update:
            await self.jishubotz.bot_settings.update_one(
                {"_id": 0}, {"$set": update}, upsert=True,
            )
            self._invalidate_bot_settings_cache()

    async def get_auto_daily_limit(self) -> int:
        doc = await self.jishubotz.bot_settings.find_one(
            {"_id": 0}, {"auto_daily_limit": 1}
        ) or {}
        return int(doc.get("auto_daily_limit", _D_AUTO_DAILY))

    async def get_manual_daily_limit(self) -> int:
        doc = await self.jishubotz.bot_settings.find_one(
            {"_id": 0}, {"manual_daily_limit": 1}
        ) or {}
        return int(doc.get("manual_daily_limit", _D_MANUAL_DAILY))

    async def get_transmission_limit(self) -> int:
        doc = await self.jishubotz.bot_settings.find_one(
            {"_id": 0}, {"transmission_limit": 1}
        ) or {}
        return int(doc.get("transmission_limit", _D_TRANSMISSION))

    # ── Manual rename daily counter ────────────────────────────────────────────

    async def get_manual_rename_today(self, user_id: int) -> int:
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        doc   = await self.col.find_one(
            {"_id": int(user_id)}, {f"manual_rename_daily.{today}": 1}
        ) or {}
        return int((doc.get("manual_rename_daily") or {}).get(today, 0))

    async def inc_manual_rename_today(self, user_id: int) -> int:
        today  = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        result = await self.col.find_one_and_update(
            {"_id": int(user_id)},
            {"$inc": {f"manual_rename_daily.{today}": 1}},
            upsert=True, return_document=True,
        )
        return int((result or {}).get("manual_rename_daily", {}).get(today, 1))

    # ─────────────────────── rename history & count ──────────────────────────

    async def record_rename(self, user_id: int, original: str, renamed: str) -> None:
        entry = {"original": original, "renamed": renamed, "ts": time.time()}
        await self.col.update_one(
            {"_id": int(user_id)},
            {"$push": {"rename_history": {"$each": [entry], "$slice": -20}}},
            upsert=True,
        )

    async def get_rename_history(self, user_id: int) -> list:
        doc = await self.col.find_one({"_id": int(user_id)}, {"rename_history": 1}) or {}
        return doc.get("rename_history", [])

    async def inc_rename_count(self, user_id: int) -> int:
        result = await self.col.find_one_and_update(
            {"_id": int(user_id)},
            {"$inc": {"total_renames": 1}},
            upsert=True, return_document=True,
        )
        return int((result or {}).get("total_renames", 1))

    async def get_rename_count(self, user_id: int) -> int:
        doc = await self.col.find_one({"_id": int(user_id)}, {"total_renames": 1}) or {}
        return int(doc.get("total_renames", 0))

    # ─────────────────────── file-to-link storage ────────────────────────────

    async def save_file(self, doc: dict) -> None:
        await self.jishubotz.files.insert_one(doc)

    async def get_file(self, token: str) -> dict | None:
        doc = await self.jishubotz.files.find_one({"_id": token})
        if not doc:
            return None
        expires = doc.get("expires_at")
        if expires and expires < time.time():
            await self.jishubotz.files.delete_one({"_id": token})
            return None
        return doc

    async def get_user_files(self, user_id: int, limit: int = 20) -> list:
        now    = time.time()
        cursor = self.jishubotz.files.find(
            {"user_id": int(user_id), "batch_id": None}
        ).sort("created_at", -1).limit(limit)
        result = []
        async for doc in cursor:
            expires = doc.get("expires_at")
            if expires and expires < now:
                await self.jishubotz.files.delete_one({"_id": doc["_id"]})
                continue
            result.append(doc)
        return result

    async def delete_user_file(self, token: str, user_id: int) -> bool:
        r = await self.jishubotz.files.delete_one({"_id": token, "user_id": int(user_id)})
        return r.deleted_count > 0

    async def delete_all_user_files(self, user_id: int) -> int:
        r = await self.jishubotz.files.delete_many({"user_id": int(user_id)})
        return r.deleted_count

    async def count_user_files(self, user_id: int) -> int:
        return await self.jishubotz.files.count_documents({"user_id": int(user_id)})

    # ─────────────────────── batch storage ──────────────────────────────────

    async def save_batch(self, doc: dict) -> None:
        await self.jishubotz.batches.insert_one(doc)

    async def get_batch(self, batch_id: str) -> dict | None:
        doc = await self.jishubotz.batches.find_one({"_id": batch_id})
        if not doc:
            return None
        expires = doc.get("expires_at")
        if expires and expires < time.time():
            await self.jishubotz.batches.delete_one({"_id": batch_id})
            return None
        return doc

    # ─────────────────────── link expiry ────────────────────────────────────

    # ─────────────────────── rename concurrency ──────────────────────────────

    async def get_rename_concurrency(self) -> int:
        """Return the single rename-queue concurrency value (default 4)."""
        return int(await self.get_bot_setting("rename_concurrency", 4))

    async def set_rename_concurrency(self, n: int) -> None:
        """Persist the rename-queue concurrency value."""
        await self.set_bot_setting("rename_concurrency", max(1, int(n)))

    async def get_expiry_minutes(self) -> int:
        return int(await self.get_bot_setting("link_expiry_minutes", 0))

    async def set_expiry_minutes(self, minutes: int) -> None:
        await self.set_bot_setting("link_expiry_minutes", int(minutes))

    # ─────────────────────── rename mode ────────────────────────────────────

    async def set_rename_mode(self, user_id: int, mode: str) -> None:
        await self._set(user_id, "rename_mode", mode)

    async def get_rename_mode(self, user_id: int) -> str:
        return await self._get(user_id, "rename_mode", "manual")

    # ─────────────────────── auto-rename template ────────────────────────────

    async def set_format_template(self, user_id: int, template: str) -> None:
        await self._set(user_id, "format_template", template)

    async def get_format_template(self, user_id: int):
        return await self._get(user_id, "format_template")

    # ─────────────────────── auto-rename source ──────────────────────────────

    async def get_global_rename_source(self) -> str:
        """Global extraction source — set by admin, applies to all users."""
        doc = await self._get_bot_settings()
        return doc.get("global_rename_source", "filename")

    async def set_global_rename_source(self, source: str) -> None:
        await self.set_bot_setting("global_rename_source", source)

    async def set_rename_source(self, user_id: int, source: str) -> None:
        await self._set(user_id, "rename_source", source)

    async def get_rename_source(self, user_id: int) -> str:
        return await self._get(user_id, "rename_source", "filename")

    # ─────────────────────── auto-rename daily counter ───────────────────────

    async def get_auto_rename_today(self, user_id: int) -> int:
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        doc   = await self.col.find_one(
            {"_id": int(user_id)}, {f"auto_rename_daily.{today}": 1}
        ) or {}
        return int((doc.get("auto_rename_daily") or {}).get(today, 0))

    async def inc_auto_rename_today(self, user_id: int) -> int:
        today  = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        result = await self.col.find_one_and_update(
            {"_id": int(user_id)},
            {"$inc": {f"auto_rename_daily.{today}": 1}},
            upsert=True, return_document=True,
        )
        return int((result or {}).get("auto_rename_daily", {}).get(today, 1))

    # ─────────────────────── auto-rename media preference ────────────────────

    async def set_media_preference(self, user_id: int, media_type: str) -> None:
        await self._set(user_id, "auto_media_type", media_type)

    async def get_media_preference(self, user_id: int):
        return await self._get(user_id, "auto_media_type")

    # ═══════════════════════════════════════════════════════════════════════
    # Pending auto-rename queue  (restart persistence)
    # ═══════════════════════════════════════════════════════════════════════

    async def save_pending_job(
        self,
        job_id: str, user_id: int, chat_id: int,
        message_id: int, file_name: str, queued_at: float,
        username: str = "",
        job_type: str = "auto",          # "auto" | "manual"
        new_filename: str = "",          # desired output name (manual rename)
        upload_type: str = "",           # "document" | "video" etc (manual rename)
        format_template: str = "",       # user's rename template (auto rename)
    ) -> None:
        await self.jishubotz.pending_jobs.update_one(
            {"_id": job_id},
            {"$set": {
                "user_id":         int(user_id),
                "chat_id":         int(chat_id),
                "message_id":      int(message_id),
                "file_name":       str(file_name),
                "queued_at":       float(queued_at),
                "username":        str(username) if username else "",
                "job_type":        str(job_type),
                "new_filename":    str(new_filename),
                "upload_type":     str(upload_type),
                "format_template": str(format_template),
            }},
            upsert=True,
        )

    async def delete_pending_job(self, job_id: str) -> None:
        await self.jishubotz.pending_jobs.delete_one({"_id": job_id})

    async def load_all_pending_jobs(self) -> list[dict]:
        cursor = self.jishubotz.pending_jobs.find({}).sort("queued_at", 1)
        return await cursor.to_list(length=None)

    async def clear_all_pending_jobs(self) -> None:
        await self.jishubotz.pending_jobs.delete_many({})

    # ═══════════════════════════════════════════════════════════════════════
    # Indexes  (idempotent)
    # ═══════════════════════════════════════════════════════════════════════

    # ─────────────────────── global metadata ─────────────────────────────────
    # User per-field settings are NEVER modified by global metadata.

    async def get_global_metadata_enabled(self) -> bool:
        """Return True when global metadata override is active."""
        return bool(await self.get_bot_setting("global_metadata_enabled", False))

    async def set_global_metadata_enabled(self, enabled: bool) -> None:
        """Enable or disable global metadata override (persisted)."""
        await self.set_bot_setting("global_metadata_enabled", bool(enabled))

    async def get_global_metadata_fields(self) -> dict:
        """Return the owner-configured global metadata fields."""
        raw = await self.get_bot_setting("global_metadata_fields", {})
        return {**_META_DEFAULTS, **(raw or {})}

    async def set_global_metadata_field(self, field: str, value: str) -> None:
        """Set a single field in the global metadata configuration."""
        existing = await self.get_bot_setting("global_metadata_fields", {}) or {}
        existing[field] = value
        await self.set_bot_setting("global_metadata_fields", existing)

    async def set_global_metadata_fields(self, fields: dict) -> None:
        """Replace the entire global metadata fields dict (owner bulk-set)."""
        await self.set_bot_setting("global_metadata_fields", dict(fields))

    async def clear_global_metadata_fields(self) -> None:
        """Reset all global metadata fields to empty strings."""
        await self.set_bot_setting("global_metadata_fields", dict(_META_DEFAULTS))

    async def get_effective_metadata(self, user_id: int) -> tuple[bool, dict]:
        """
        Return the metadata (enabled, fields) that should be applied for a job.

        Checks global override first; falls back to per-user settings.
        Returns:
            (use_metadata: bool, fields: dict)
        """
        global_on = await self.get_global_metadata_enabled()
        if global_on:
            g_fields  = await self.get_global_metadata_fields()
            has_values = any((v or "").strip() for v in g_fields.values())
            return has_values, g_fields

        # Fall through to per-user settings
        doc = await self.col.find_one(
            {"_id": int(user_id)}, {"metadata": 1, "metadata_fields": 1}
        ) or {}
        user_enabled = bool(doc.get("metadata", False))
        raw_fields   = doc.get("metadata_fields") or {}
        return user_enabled, {**_META_DEFAULTS, **raw_fields}

    async def ensure_indexes(self) -> None:
        await self.jishubotz.pending_jobs.create_index(
            [("queued_at", 1)], name="queued_at_asc", background=True
        )
        await self.bannedList.create_index(
            [("banId", 1)], name="banId_asc", background=True, unique=True
        )
        await self.jishubotz.files.create_index(
            [("user_id", 1), ("created_at", -1)], name="user_files", background=True
        )
        await self.col.create_index(
            [("premium", 1), ("premium_expiry", 1)],
            name="premium_expiry", background=True,
        )


# Module-level singleton
jishubotz = Database(Config.DB_URL, Config.DB_NAME)
