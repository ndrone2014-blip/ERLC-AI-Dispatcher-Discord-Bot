"""Attaching/detaching units to incidents and backup requests.

Every method returns only after the database write succeeded, so callers (voice,
panels, commands) can safely confirm the action to officers.
"""
from __future__ import annotations

import logging
from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import iso_now
from security.audit import AuditLog

from .incidents import IncidentError, IncidentService
from .units import UnitError, UnitService

log = logging.getLogger(__name__)


class AssignmentService:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog, incidents: IncidentService, units: UnitService):
        self.db = db
        self.bus = bus
        self.audit = audit
        self.incidents = incidents
        self.units = units

    async def attach(self, incident_id: int, unit_id: int, actor_id: Optional[int]) -> tuple[dict, dict, Optional[dict]]:
        """Attach a unit. Returns (incident, unit, previous_incident_or_None)."""
        inc = await self.incidents.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        if inc["status"] == "closed":
            raise IncidentError(f"Incident #{inc['number']} is closed")
        unit = await self.units.unit(unit_id)
        if unit is None:
            raise UnitError("Unit not found")
        if unit.get("availability") == "off":
            raise UnitError(f"{unit['callsign']} is out of service. Change status before attaching.")

        previous = None
        if unit.get("current_incident_id") == incident_id:
            return inc, unit, None
        if unit.get("current_incident_id"):
            previous = await self.incidents.get(unit["current_incident_id"])
            await self.detach(unit["current_incident_id"], unit_id, actor_id, reason="reassigned")

        now = iso_now()
        enroute = await self.units.code_for_availability(inc["guild_id"], "assigned")
        async with self.db.transaction() as c:
            await c.execute(
                "INSERT INTO incident_assignments(guild_id, incident_id, unit_id, assigned_by, assigned_at) VALUES (?, ?, ?, ?, ?)",
                (inc["guild_id"], incident_id, unit_id, actor_id, now),
            )
            await c.execute("UPDATE units SET current_incident_id = ? WHERE id = ?", (incident_id, unit_id))
            if inc["status"] == "pending":
                await c.execute("UPDATE incidents SET status = 'active', updated_at = ? WHERE id = ?", (now, incident_id))
            await c.execute(
                "UPDATE announcements SET status = 'cancelled', last_error = 'unit responded' "
                "WHERE incident_id = ? AND status = 'queued' AND kind = 'repeat'", (incident_id,))
        if enroute and unit.get("availability") in {"available", "unavailable"}:
            await self.units.set_status(unit_id, enroute, actor_id)
        await self.incidents.add_history(inc, "unit_attached", actor_id, unit=unit["callsign"])
        await self.audit.record(inc["guild_id"], actor_id, "incident.attach", f"incident:{inc['number']}", {"unit": unit["callsign"]})
        await self.bus.publish(ev.ASSIGNMENT_CHANGED, inc["guild_id"], incident_id=incident_id, unit_id=unit_id, action="attach",
                               actor_id=actor_id)
        await self.bus.publish(ev.ACTIVITY, inc["guild_id"], text=f"{unit['callsign']} attached to incident #{inc['number']}")
        return await self.incidents.get(incident_id), await self.units.unit(unit_id), previous

    async def detach(self, incident_id: int, unit_id: int, actor_id: Optional[int], reason: str = "cleared") -> dict:
        inc = await self.incidents.get(incident_id)
        if inc is None:
            raise IncidentError("Incident not found")
        unit = await self.units.unit(unit_id)
        if unit is None:
            raise UnitError("Unit not found")
        row = await self.db.fetchone(
            "SELECT id FROM incident_assignments WHERE incident_id = ? AND unit_id = ? AND cleared_at IS NULL", (incident_id, unit_id))
        if row is None:
            raise UnitError(f"{unit['callsign']} is not attached to incident #{inc['number']}")
        now = iso_now()
        async with self.db.transaction() as c:
            await c.execute("UPDATE incident_assignments SET cleared_at = ? WHERE id = ?", (now, row["id"]))
            await c.execute("UPDATE units SET current_incident_id = NULL WHERE id = ? AND current_incident_id = ?", (unit_id, incident_id))
        await self.incidents.add_history(inc, "unit_detached", actor_id, unit=unit["callsign"], reason=reason)
        await self.audit.record(inc["guild_id"], actor_id, "incident.detach", f"incident:{inc['number']}", {"unit": unit["callsign"], "reason": reason})
        await self.bus.publish(ev.ASSIGNMENT_CHANGED, inc["guild_id"], incident_id=incident_id, unit_id=unit_id, action="detach",
                               actor_id=actor_id, reason=reason)
        await self.bus.publish(ev.ACTIVITY, inc["guild_id"], text=f"{unit['callsign']} cleared from incident #{inc['number']}")
        return await self.units.unit(unit_id)

    async def set_unit_status(self, unit_id: int, code: str, actor_id: Optional[int], officer_id: Optional[int] = None) -> dict:
        """Change status; going available / out of service clears the unit from its incident."""
        unit = await self.units.unit(unit_id)
        if unit is None:
            raise UnitError("Unit not found")
        sc = await self.units.status_code(unit["guild_id"], code)
        if sc is None:
            raise UnitError(f"Status code '{code}' is not configured on this server.")
        if sc["availability"] in {"available", "off"} and unit.get("current_incident_id"):
            await self.detach(unit["current_incident_id"], unit_id, actor_id, reason=f"status {sc['code']}")
        return await self.units.set_status(unit_id, sc["code"], actor_id, officer_id)

    # ------------------------------------------------------------------ backup
    async def request_backup(self, guild_id: int, unit_id: Optional[int], incident_id: Optional[int], actor_id: int,
                             urgency: str = "routine", note: str = "") -> dict:
        if urgency not in {"routine", "urgent", "emergency"}:
            urgency = "routine"
        if incident_id is None and unit_id is not None:
            u = await self.units.unit(unit_id)
            incident_id = u.get("current_incident_id") if u else None
        existing = await self.db.fetchone(
            "SELECT * FROM backup_requests WHERE guild_id = ? AND status = 'open' AND unit_id IS ? AND incident_id IS ? "
            "AND created_at > strftime('%Y-%m-%dT%H:%M:%fZ','now','-60 seconds')",
            (guild_id, unit_id, incident_id),
        )
        if existing:
            return dict(existing) | {"duplicate": True}
        req_id = await self.db.execute(
            "INSERT INTO backup_requests(guild_id, incident_id, unit_id, requested_by, urgency, note) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, incident_id, unit_id, actor_id, urgency, note),
        )
        if incident_id:
            await self.incidents.add_history(incident_id, "backup_requested", actor_id, urgency=urgency, note=note)
        await self.audit.record(guild_id, actor_id, "backup.request", f"backup:{req_id}", {"incident_id": incident_id, "urgency": urgency})
        await self.bus.publish(ev.BACKUP_REQUESTED, guild_id, request_id=req_id, incident_id=incident_id, unit_id=unit_id, urgency=urgency)
        return dict(await self.db.fetchone("SELECT * FROM backup_requests WHERE id = ?", (req_id,)))

    async def open_backup_requests(self, guild_id: int) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT b.*, u.callsign, i.number AS incident_number FROM backup_requests b "
            "LEFT JOIN units u ON u.id = b.unit_id LEFT JOIN incidents i ON i.id = b.incident_id "
            "WHERE b.guild_id = ? AND b.status = 'open' ORDER BY b.id DESC", (guild_id,))
        return [dict(r) for r in rows]

    async def resolve_backup(self, request_id: int, actor_id: int) -> None:
        row = await self.db.fetchone("SELECT guild_id FROM backup_requests WHERE id = ?", (request_id,))
        if row is None:
            return
        await self.db.execute(
            "UPDATE backup_requests SET status = 'resolved', resolved_at = ?, resolved_by = ? WHERE id = ?",
            (iso_now(), actor_id, request_id))
        await self.audit.record(row["guild_id"], actor_id, "backup.resolve", f"backup:{request_id}")
        await self.bus.publish(ev.INCIDENT_UPDATED, row["guild_id"], changes=["backup"])
