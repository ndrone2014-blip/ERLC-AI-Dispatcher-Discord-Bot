"""Database-backed announcement queue (survives restarts and voice disconnects)."""
from __future__ import annotations

import logging
from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import ANNOUNCE_PRIORITY, ANNOUNCEMENT_TYPES, PRIORITY_RANK, iso_now

log = logging.getLogger(__name__)

# Which admin toggle controls each announcement kind. None = always allowed.
KIND_TOGGLE = {
    "new_911": "new_911",
    "repeat": "new_911",
    "new_report": "new_report",
    "unit_assignment": "unit_assignment",
    "backup_request": "backup_request",
    "priority_change": "priority_change",
    "escalation": "priority_change",
    "incident_closure": "incident_closure",
    "bolo": "bolo",
    "department_notice": "department_notice",
    "voice_reply": None,
    "test": None,
}

ANNOUNCEMENT_UPDATED = "announcement.updated"


def priority_for(kind: str, incident_priority: Optional[str] = None) -> tuple[int, bool]:
    base = ANNOUNCE_PRIORITY.get(kind, 10)
    critical = False
    if incident_priority:
        rank = PRIORITY_RANK.get(incident_priority, 1)
        base += rank * 5
        if incident_priority == "critical" and kind in {"new_911", "escalation", "repeat"}:
            base = ANNOUNCE_PRIORITY["critical"]
            critical = True
    if kind == "backup_request":
        critical = critical or False
    return base, critical


class AnnouncementService:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus

    async def toggles(self, guild_id: int) -> dict[str, bool]:
        stored = await self.db.get_config(guild_id, "announce_toggles", {}) or {}
        # ALL eight announcement types are enabled by default.
        return {k: bool(stored.get(k, True)) for k in ANNOUNCEMENT_TYPES}

    async def set_toggle(self, guild_id: int, kind: str, enabled: bool) -> None:
        if kind not in ANNOUNCEMENT_TYPES:
            raise ValueError(kind)
        stored = await self.db.get_config(guild_id, "announce_toggles", {}) or {}
        stored[kind] = bool(enabled)
        await self.db.set_config(guild_id, "announce_toggles", stored)
        await self.bus.publish(ev.CONFIG_CHANGED, guild_id, what="announcements")

    async def enqueue(self, guild_id: int, kind: str, text: str, *, dedupe_key: str, incident_id: Optional[int] = None,
                      incident_priority: Optional[str] = None, priority: Optional[int] = None, critical: Optional[bool] = None) -> Optional[int]:
        toggle = KIND_TOGGLE.get(kind, "department_notice")
        if toggle is not None and not (await self.toggles(guild_id)).get(toggle, True):
            log.info("Announcement %s suppressed (toggle %s disabled)", dedupe_key, toggle)
            return None
        p, c = priority_for(kind, incident_priority)
        if priority is not None:
            p = priority
        if critical is not None:
            c = critical
        before = await self.db.fetchone("SELECT id FROM announcements WHERE guild_id = ? AND dedupe_key = ?", (guild_id, dedupe_key))
        if before:
            return None  # duplicate prevention
        await self.db.execute(
            "INSERT OR IGNORE INTO announcements(guild_id, kind, incident_id, text, priority, critical, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, kind, incident_id, text, p, 1 if c else 0, dedupe_key),
        )
        row = await self.db.fetchone("SELECT id FROM announcements WHERE guild_id = ? AND dedupe_key = ?", (guild_id, dedupe_key))
        if row is None:
            return None
        await self.bus.publish(ANNOUNCEMENT_UPDATED, guild_id, announcement_id=row["id"], status="queued")
        return row["id"]

    async def next_queued(self, guild_id: int) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT * FROM announcements WHERE guild_id = ? AND status = 'queued' ORDER BY priority DESC, id ASC LIMIT 1", (guild_id,))
        return dict(row) if row else None

    async def pending_count(self, guild_id: int) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM announcements WHERE guild_id = ? AND status = 'queued'", (guild_id,), 0))

    async def mark(self, ann_id: int, status: str, error: Optional[str] = None) -> None:
        if status == "played":
            await self.db.execute("UPDATE announcements SET status = 'played', played_at = ?, attempts = attempts + 1 WHERE id = ?",
                                  (iso_now(), ann_id))
        elif status == "failed":
            await self.db.execute(
                "UPDATE announcements SET attempts = attempts + 1, last_error = ?, status = CASE WHEN attempts + 1 >= 3 THEN 'failed' ELSE 'queued' END WHERE id = ?",
                (error, ann_id))
        else:
            await self.db.execute("UPDATE announcements SET status = ?, last_error = COALESCE(?, last_error) WHERE id = ?", (status, error, ann_id))
        row = await self.db.fetchone("SELECT guild_id, status FROM announcements WHERE id = ?", (ann_id,))
        if row:
            await self.bus.publish(ANNOUNCEMENT_UPDATED, row["guild_id"], announcement_id=ann_id, status=row["status"])

    async def recover_interrupted(self) -> int:
        """On startup, anything stuck in 'playing' (crash mid-playback) goes back to the queue."""
        return await self.db.execute_rowcount("UPDATE announcements SET status = 'queued' WHERE status = 'playing'")

    async def cancel_for_incident(self, incident_id: int, kinds: tuple[str, ...] = ("repeat",)) -> None:
        placeholders = ",".join("?" for _ in kinds)
        await self.db.execute(
            f"UPDATE announcements SET status = 'cancelled' WHERE incident_id = ? AND status = 'queued' AND kind IN ({placeholders})",
            (incident_id, *kinds))

    async def recent(self, guild_id: int, limit: int = 10) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM announcements WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit))
        return [dict(r) for r in rows]

    async def play_stats(self, incident_id: int) -> tuple[int, Optional[str]]:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, MAX(played_at) AS last FROM announcements WHERE incident_id = ? AND kind IN ('new_911','repeat') AND status IN ('played','muted')",
            (incident_id,))
        return (int(row["n"] or 0), row["last"]) if row else (0, None)

    async def has_queued(self, incident_id: int, kinds: tuple[str, ...]) -> bool:
        placeholders = ",".join("?" for _ in kinds)
        return await self.db.fetchone(
            f"SELECT 1 FROM announcements WHERE incident_id = ? AND status IN ('queued','playing') AND kind IN ({placeholders})",
            (incident_id, *kinds)) is not None
