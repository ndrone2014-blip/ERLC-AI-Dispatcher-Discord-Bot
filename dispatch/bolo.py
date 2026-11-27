"""BOLO (Be On the Look Out) management."""
from __future__ import annotations

from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import dumps, iso_now, loads
from security.audit import AuditLog
from security.validation import ValidationError, clean_text

BOLO_PRIORITIES = ("low", "medium", "high", "critical")
BOLO_KINDS = ("player", "person", "vehicle", "other")


class BoloService:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db = db
        self.bus = bus
        self.audit = audit

    async def create(self, guild_id: int, actor_id: int, *, kind: str, subject: str, description: str = "",
                     plate: str = "", last_seen: str = "", reason: str = "", priority: str = "medium",
                     roblox_id: Optional[int] = None, roblox_username: str = "") -> dict:
        kind = kind if kind in BOLO_KINDS else "other"
        priority = priority if priority in BOLO_PRIORITIES else "medium"
        subject = clean_text(subject, 100, required=True, field="Subject")
        number = await self.db.next_number(guild_id, "bolo")
        bolo_id = await self.db.execute(
            "INSERT INTO bolos(guild_id, number, kind, subject, description, plate, last_seen, reason, created_by, priority, roblox_id, "
            "roblox_username) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, kind, subject, clean_text(description, 800), clean_text(plate, 20).upper() or None,
             clean_text(last_seen, 150), clean_text(reason, 300), actor_id, priority, int(roblox_id) if roblox_id else None,
             clean_text(roblox_username, 20) or None),
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
        allowed = {"subject": 100, "description": 800, "plate": 20, "last_seen": 150, "reason": 300, "roblox_username": 20}
        changes: dict = {k: clean_text(v, allowed[k]) for k, v in fields.items() if k in allowed and v is not None}
        if "subject" in changes and not changes["subject"]:
            raise ValidationError("Subject is required.")
        if fields.get("priority") is not None:
            if fields["priority"] not in BOLO_PRIORITIES:
                raise ValidationError("Unknown priority.")
            changes["priority"] = fields["priority"]
        if fields.get("kind") is not None:
            changes["kind"] = fields["kind"] if fields["kind"] in BOLO_KINDS else "other"
        if "roblox_id" in fields:
            changes["roblox_id"] = int(fields["roblox_id"]) if fields["roblox_id"] else None
        if "plate" in changes:
            changes["plate"] = changes["plate"].upper() or None
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
        if bolo["status"] != "active":
            raise ValueError("This BOLO is already closed")
        await self.db.execute("UPDATE bolos SET status = 'cleared', cleared_by = ?, cleared_at = ?, updated_at = ?, close_reason = ? WHERE id = ?",
                              (actor_id, iso_now(), iso_now(), clean_text(reason, 300) or None, bolo_id))
        await self._history(bolo["guild_id"], bolo_id, actor_id, "cleared", reason=reason)
        await self.audit.record(bolo["guild_id"], actor_id, "bolo.clear", f"bolo:{bolo['number']}")
        await self.bus.publish(ev.BOLO_CHANGED, bolo["guild_id"], bolo_id=bolo_id, action="cleared")
        return await self.get(bolo_id)

    async def reopen(self, bolo_id: int, actor_id: int) -> dict:
        bolo = await self.get(bolo_id)
        if bolo is None:
            raise ValueError("BOLO not found")
        if bolo["status"] == "active":
            raise ValueError("This BOLO is already active")
        await self.db.execute("UPDATE bolos SET status = 'active', cleared_by = NULL, cleared_at = NULL, close_reason = NULL, updated_at = ? "
                              "WHERE id = ?", (iso_now(), bolo_id))
        await self._history(bolo["guild_id"], bolo_id, actor_id, "reopened")
        await self.audit.record(bolo["guild_id"], actor_id, "bolo.reopen", f"bolo:{bolo['number']}")
        await self.bus.publish(ev.BOLO_CHANGED, bolo["guild_id"], bolo_id=bolo_id, action="reopened")
        return await self.get(bolo_id)

    async def add_note(self, bolo_id: int, actor_id: int, note: str) -> int:
        bolo = await self.get(bolo_id)
        if bolo is None:
            raise ValueError("BOLO not found")
        text = clean_text(note, 1000, required=True, field="Note")
        nid = await self.db.execute("INSERT INTO bolo_notes(guild_id, bolo_id, author_id, note) VALUES (?, ?, ?, ?)",
                                    (bolo["guild_id"], bolo_id, actor_id, text))
        await self._history(bolo["guild_id"], bolo_id, actor_id, "note")
        await self.bus.publish(ev.BOLO_CHANGED, bolo["guild_id"], bolo_id=bolo_id, action="noted")
        return nid

    async def notes(self, bolo_id: int) -> list[dict]:
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM bolo_notes WHERE bolo_id = ? ORDER BY id DESC", (bolo_id,))]

    async def search(self, guild_id: int, *, status: str = "active", q: str = "", priority: str = "", kind: str = "",
                     page: int = 1, per_page: int = 25) -> dict:
        where, params = ["guild_id = ?"], [guild_id]
        if status in {"active", "cleared"}:
            where.append("status = ?")
            params.append(status)
        if priority in BOLO_PRIORITIES:
            where.append("priority = ?")
            params.append(priority)
        if kind in BOLO_KINDS:
            where.append("kind = ?")
            params.append(kind)
        if q:
            where.append("(subject LIKE ? OR description LIKE ? OR plate LIKE ? OR roblox_username LIKE ? OR reason LIKE ? "
                         "OR CAST(number AS TEXT) = ? OR CAST(roblox_id AS TEXT) = ?)")
            params += [f"%{q}%"] * 5 + [q.lstrip("#"), q]
        base = " FROM bolos WHERE " + " AND ".join(where)
        order = ("ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id DESC"
                 if status == "active" else "ORDER BY id DESC")
        page, per_page = max(1, int(page or 1)), max(1, min(100, int(per_page or 25)))
        total = int(await self.db.fetchval("SELECT COUNT(*)" + base, params, 0))
        rows = await self.db.fetchall(f"SELECT *{base} {order} LIMIT ? OFFSET ?", params + [per_page, (page - 1) * per_page])
        return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page,
                "pages": max(1, -(-total // per_page))}

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
