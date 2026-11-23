"""Incident records: creation, updates, notes, priority approval, closure, history."""
from __future__ import annotations

import logging
from typing import Any, Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import PRIORITIES, PRIORITY_RANK, dumps, iso_now, loads
from security.audit import AuditLog

log = logging.getLogger(__name__)


class IncidentError(Exception):
    pass


def incident_label(inc: dict) -> str:
    return f"#{inc['number']}"


class IncidentService:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db = db
        self.bus = bus
        self.audit = audit

    # ------------------------------------------------------------------ queries
    async def get(self, incident_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        return dict(row) if row else None

    async def get_by_number(self, guild_id: int, number: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM incidents WHERE guild_id = ? AND number = ?", (guild_id, number))
        return dict(row) if row else None

    async def list_active(self, guild_id: int, limit: int = 50) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM incidents WHERE guild_id = ? AND status != 'closed' ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )
        out = [dict(r) for r in rows]
        out.sort(key=lambda i: (-PRIORITY_RANK.get(i.get("priority") or "routine", 1), i["id"]))
        return out

    async def list_recent_closed(self, guild_id: int, limit: int = 10) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM incidents WHERE guild_id = ? AND status = 'closed' ORDER BY closed_at DESC LIMIT ?",
            (guild_id, limit),
        )
        return [dict(r) for r in rows]

    async def history(self, incident_id: int, limit: int = 50) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM incident_history WHERE incident_id = ? ORDER BY id ASC LIMIT ?", (incident_id, limit)
        )
        return [dict(r) | {"detail": loads(r["detail"], {})} for r in rows]

    async def assigned_units(self, incident_id: int) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT u.*, d.short_code AS dept_code, d.name AS dept_name, a.assigned_at "
            "FROM incident_assignments a JOIN units u ON u.id = a.unit_id "
            "LEFT JOIN departments d ON d.id = u.department_id "
            "WHERE a.incident_id = ? AND a.cleared_at IS NULL ORDER BY a.assigned_at",
            (incident_id,),
        )
        return [dict(r) for r in rows]

    async def highest_priority(self, guild_id: int) -> Optional[dict]:
        active = await self.list_active(guild_id)
        return active[0] if active else None

    # ------------------------------------------------------------------ mutations
    async def add_history(self, incident: dict | int, event_type: str, actor_id: Optional[int] = None, **detail: Any) -> None:
        inc = incident if isinstance(incident, dict) else await self.get(incident)
        if inc is None:
            return
        await self.db.execute(
            "INSERT INTO incident_history(guild_id, incident_id, actor_id, event_type, detail) VALUES (?, ?, ?, ?, ?)",
            (inc["guild_id"], inc["id"], actor_id, event_type, dumps(detail)),
        )

    async def create(
        self,
        guild_id: int,
        *,
        source: str,
        verification: str,
        nature: Optional[str],
        description: Optional[str] = None,
        location: Optional[str] = None,
        postal: Optional[str] = None,
        street: Optional[str] = None,
        position: Optional[tuple[float, float]] = None,
        caller_display: Optional[str] = None,
        category: Optional[str] = None,
        requested_department: Optional[str] = None,
        erlc_call_number: Optional[int] = None,
        erlc_started_at: Optional[int] = None,
        erlc_team: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> dict:
        if source not in {"erlc", "discord", "manual", "test"}:
            raise IncidentError(f"Invalid incident source {source}")
        if verification not in {"verified", "unverified", "test"}:
            raise IncidentError(f"Invalid verification {verification}")
        # Only the ER:LC sync path may create API-verified incidents.
        if verification == "verified" and source != "erlc":
            raise IncidentError("Only incidents detected through the ER:LC API can be marked verified")
        if source == "test" and verification != "test":
            raise IncidentError("Simulated incidents must be labelled as test data")

        number = await self.db.next_number(guild_id, "incident")
        inc_id = await self.db.execute(
            "INSERT INTO incidents(guild_id, number, source, verification, status, category, nature, description, location, postal, street, "
            "position_x, position_z, caller_display, requested_department, erlc_call_number, erlc_started_at, erlc_team, created_by) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, number, source, verification, category, nature, description, location, postal, street,
                position[0] if position else None, position[1] if position else None,
                caller_display, requested_department, erlc_call_number, erlc_started_at, erlc_team, created_by,
            ),
        )
        inc = await self.get(inc_id)
        await self.add_history(inc, "created", created_by, source=source, verification=verification,
                               location=location, nature=nature)
        await self.audit.record(guild_id, created_by, "incident.create", f"incident:{number}", {"source": source})
        await self.bus.publish(ev.INCIDENT_CREATED, guild_id, incident_id=inc_id)
        await self.bus.publish(ev.ACTIVITY, guild_id, text=f"New incident #{number} ({source}) — {nature or 'Unknown nature'}")
        return inc

    async def update_fields(self, incident_id: int, actor_id: Optional[int], **fields: Any) -> dict:
        allowed = {"nature", "description", "location", "postal", "street", "category", "caller_display", "status"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        inc = await self.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        if inc["status"] == "closed" and changes.get("status") != "active":
            raise IncidentError("Incident is closed")
        if not changes:
            return inc
        sets = ", ".join(f"{k} = ?" for k in changes)
        await self.db.execute(
            f"UPDATE incidents SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), iso_now(), incident_id)
        )
        before = {k: inc.get(k) for k in changes}
        await self.add_history(inc, "updated", actor_id, before=before, after=changes)
        await self.audit.record(inc["guild_id"], actor_id, "incident.update", f"incident:{inc['number']}", changes)
        await self.bus.publish(ev.INCIDENT_UPDATED, inc["guild_id"], incident_id=incident_id, changes=list(changes))
        return await self.get(incident_id)

    async def add_note(self, incident_id: int, actor_id: Optional[int], note: str, *, author: str = "") -> None:
        inc = await self.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        await self.add_history(inc, "note", actor_id, note=note, author=author)
        await self.db.execute("UPDATE incidents SET updated_at = ? WHERE id = ?", (iso_now(), incident_id))
        await self.bus.publish(ev.INCIDENT_UPDATED, inc["guild_id"], incident_id=incident_id, changes=["note"], note=note)

    async def notes(self, incident_id: int) -> list[dict]:
        return [h for h in await self.history(incident_id, 200) if h["event_type"] == "note"]

    async def apply_assessment(self, incident_id: int, assessment: dict, *, approval_threshold: str, reason: str = "") -> tuple[dict, dict]:
        """Store an assessment. Returns (old_incident, new_incident).

        The official `priority` is only changed automatically while it has not been
        approved/overridden by a supervisor, and only below the approval threshold.
        """
        inc = await self.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        prio = assessment["priority"]
        needs_approval = PRIORITY_RANK[prio] >= PRIORITY_RANK.get(approval_threshold, PRIORITY_RANK["high"])
        updates: dict[str, Any] = {
            "ai_priority": prio,
            "recommended_units": assessment["recommended_units"],
            "recommended_departments": dumps(assessment["departments"]),
            "supervisor_recommended": 1 if assessment.get("supervisor_notification") else 0,
            "recommendation_reason": assessment.get("reasoning"),
            "assessment_source": assessment.get("source"),
            "assessment_json": dumps(assessment),
            "category": inc.get("category") or assessment.get("category"),
        }
        if inc["priority_status"] == "recommended" or inc["priority"] is None:
            updates["priority"] = prio
            updates["priority_status"] = "recommended" if needs_approval else "approved"
            if not needs_approval:
                updates["priority_approved_by"] = None
                updates["priority_approved_at"] = iso_now()
                updates["priority_reason"] = "Auto-approved (below supervisor-approval threshold)"
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self.db.execute(f"UPDATE incidents SET {sets}, updated_at = ? WHERE id = ?", (*updates.values(), iso_now(), incident_id))
        await self.add_history(inc, "assessment", None, assessment=assessment, reason=reason,
                               previous={"ai_priority": inc.get("ai_priority"), "recommended_units": inc.get("recommended_units"),
                                         "departments": loads(inc.get("recommended_departments"), [])})
        new = await self.get(incident_id)
        await self.bus.publish(ev.INCIDENT_ASSESSED, inc["guild_id"], incident_id=incident_id)
        return inc, new

    async def set_priority(self, incident_id: int, priority: str, actor_id: int, reason: Optional[str] = None) -> dict:
        if priority not in PRIORITIES:
            raise IncidentError("Invalid priority")
        inc = await self.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        status = "approved" if priority == inc.get("ai_priority") else "overridden"
        await self.db.execute(
            "UPDATE incidents SET priority = ?, priority_status = ?, priority_approved_by = ?, priority_approved_at = ?, "
            "priority_reason = ?, updated_at = ? WHERE id = ?",
            (priority, status, actor_id, iso_now(), reason, iso_now(), incident_id),
        )
        await self.add_history(inc, "priority", actor_id, old=inc.get("priority"), new=priority,
                               ai_recommendation=inc.get("ai_priority"), status=status, reason=reason)
        await self.audit.record(inc["guild_id"], actor_id, "incident.priority", f"incident:{inc['number']}",
                                {"old": inc.get("priority"), "new": priority, "status": status, "reason": reason})
        await self.bus.publish(ev.INCIDENT_UPDATED, inc["guild_id"], incident_id=incident_id, changes=["priority"],
                               old_priority=inc.get("priority"), new_priority=priority)
        return await self.get(incident_id)

    async def close(self, incident_id: int, actor_id: Optional[int], reason: Optional[str] = None) -> dict:
        inc = await self.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        if inc["status"] == "closed":
            raise IncidentError("Incident is already closed")
        now = iso_now()
        async with self.db.transaction() as c:
            await c.execute(
                "UPDATE incidents SET status = 'closed', closed_at = ?, closed_by = ?, close_reason = ?, updated_at = ? WHERE id = ?",
                (now, actor_id, reason, now, incident_id),
            )
            await c.execute("UPDATE incident_assignments SET cleared_at = ? WHERE incident_id = ? AND cleared_at IS NULL", (now, incident_id))
            await c.execute(
                "UPDATE units SET current_incident_id = NULL WHERE current_incident_id = ?", (incident_id,)
            )
            await c.execute("UPDATE announcements SET status = 'cancelled', last_error = 'incident closed' WHERE incident_id = ? AND status = 'queued' "
                "AND kind IN ('repeat', 'new_911', 'priority_change', 'unit_assignment', 'escalation')", (incident_id,))
            await c.execute("UPDATE backup_requests SET status = 'resolved', resolved_at = ? WHERE incident_id = ? AND status = 'open'", (now, incident_id))
        await self.add_history(inc, "closed", actor_id, reason=reason)
        await self.audit.record(inc["guild_id"], actor_id, "incident.close", f"incident:{inc['number']}", {"reason": reason})
        await self.bus.publish(ev.INCIDENT_CLOSED, inc["guild_id"], incident_id=incident_id)
        await self.bus.publish(ev.UNIT_UPDATED, inc["guild_id"])
        await self.bus.publish(ev.ACTIVITY, inc["guild_id"], text=f"Incident #{inc['number']} closed")
        return await self.get(incident_id)

    async def mark_review(self, incident_id: int, actor_id: int, outcome: str, note: str = "") -> None:
        inc = await self.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        # Supervisors can review but NEVER convert a manual report into "API verified".
        await self.add_history(inc, "supervisor_review", actor_id, outcome=outcome, note=note)
        await self.audit.record(inc["guild_id"], actor_id, "incident.review", f"incident:{inc['number']}", {"outcome": outcome})
        await self.bus.publish(ev.INCIDENT_UPDATED, inc["guild_id"], incident_id=incident_id, changes=["review"])
