"""BOLO (Be On the Look Out) management."""
from __future__ import annotations

from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import dumps, iso_now, loads
from security.audit import AuditLog
from security.validation import clean_text


class BoloService:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db = db
        self.bus = bus
        self.audit = audit

    async def create(self, guild_id: int, actor_id: int, *, kind: str, subject: str, description: str = "",
                     plate: str = "", last_seen: str = "", reason: str = "") -> dict:
        kind = kind if kind in {"person", "vehicle", "other"} else "other"
        subject = clean_text(subject, 100, required=True, field="Subject")
        number = await self.db.next_number(guild_id, "bolo")
        bolo_id = await self.db.execute(
            "INSERT INTO bolos(guild_id, number, kind, subject, description, plate, last_seen, reason, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, kind, subject, clean_text(description, 800), clean_text(plate, 20).upper() or None,
             clean_text(last_seen, 150), clean_text(reason, 300), actor_id),
        )
        await self._history(guild_id, bolo_id, actor_id, "created", subject=subject)
        await self.audit.record(guild_id, actor_id, "bolo.create", f"bolo:{number}")
        await self.bus.publish(ev.BOLO_CHANGED, guild_id, bolo_id=bolo_id, action="created")
        return await self.get(bolo_id)

    async def get(self, bolo_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM bolos WHERE id = ?", (bolo_id,))
        return dict(row) if row else None

    async def get_by_number(self, guild_id: int, number: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM bolos WHERE guild_id = ? AND number = ?", (guild_id, number))
        return dict(row) if row else None

    async def active(self, guild_id: int) -> list[dict]:
        return [dict(r) for r in await self.db.fetchall(
            "SELECT * FROM bolos WHERE guild_id = ? AND status = 'active' ORDER BY id DESC", (guild_id,))]

    async def update(self, bolo_id: int, actor_id: int, **fields) -> dict:
        allowed = {"subject": 100, "description": 800, "plate": 20, "last_seen": 150, "reason": 300}
        changes = {k: clean_text(v, allowed[k]) for k, v in fields.items() if k in allowed and v is not None}
        bolo = await self.get(bolo_id)
        if bolo is None:
            raise ValueError("BOLO not found")
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            await self.db.execute(f"UPDATE bolos SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), iso_now(), bolo_id))
            await self._history(bolo["guild_id"], bolo_id, actor_id, "updated", **changes)
            await self.audit.record(bolo["guild_id"], actor_id, "bolo.update", f"bolo:{bolo['number']}", changes)
            await self.bus.publish(ev.BOLO_CHANGED, bolo["guild_id"], bolo_id=bolo_id, action="updated")
        return await self.get(bolo_id)

    async def clear(self, bolo_id: int, actor_id: int, reason: str = "") -> dict:
        bolo = await self.get(bolo_id)
        if bolo is None:
            raise ValueError("BOLO not found")
        await self.db.execute("UPDATE bolos SET status = 'cleared', cleared_by = ?, cleared_at = ?, updated_at = ? WHERE id = ?",
                              (actor_id, iso_now(), iso_now(), bolo_id))
        await self._history(bolo["guild_id"], bolo_id, actor_id, "cleared", reason=reason)
        await self.audit.record(bolo["guild_id"], actor_id, "bolo.clear", f"bolo:{bolo['number']}")
        await self.bus.publish(ev.BOLO_CHANGED, bolo["guild_id"], bolo_id=bolo_id, action="cleared")
        return await self.get(bolo_id)

    async def history(self, bolo_id: int) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM bolo_history WHERE bolo_id = ? ORDER BY id", (bolo_id,))
        return [dict(r) | {"detail": loads(r["detail"], {})} for r in rows]

    async def recent_all(self, guild_id: int, limit: int = 15) -> list[dict]:
        return [dict(r) for r in await self.db.fetchall(
            "SELECT * FROM bolos WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit))]

    async def _history(self, guild_id: int, bolo_id: int, actor_id: int, event_type: str, **detail) -> None:
        await self.db.execute("INSERT INTO bolo_history(guild_id, bolo_id, actor_id, event_type, detail) VALUES (?, ?, ?, ?, ?)",
                              (guild_id, bolo_id, actor_id, event_type, dumps(detail)))


def bolo_spoken(b: dict) -> str:
    kind = {"person": "person", "vehicle": "vehicle"}.get(b["kind"], "subject")
    text = f"Attention all units, BOLO number {b['number']}. Be on the lookout for a {kind}: {b['subject']}."
    if b.get("plate"):
        text += f" Plate {' '.join(b['plate'])}."
    if b.get("description"):
        text += f" {b['description'][:200]}"
        if not text.endswith("."):
            text += "."
    if b.get("last_seen"):
        text += f" Last seen {b['last_seen']}."
    return text
