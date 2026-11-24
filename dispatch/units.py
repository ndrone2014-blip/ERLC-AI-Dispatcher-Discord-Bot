"""Departments, status codes, officers and units."""
from __future__ import annotations

import logging
from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import DEFAULT_DEPARTMENTS, DEFAULT_STATUS_CODES, iso_now
from security.audit import AuditLog
from security.validation import ValidationError, clean_text, validate_callsign

log = logging.getLogger(__name__)


class UnitError(Exception):
    pass


class UnitService:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db = db
        self.bus = bus
        self.audit = audit

    # ------------------------------------------------------------------ guild bootstrap
    async def ensure_defaults(self, guild_id: int, guild_name: str = "") -> None:
        await self.db.execute(
            "INSERT INTO guilds(guild_id, name) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name",
            (guild_id, guild_name),
        )
        if not await self.db.fetchone("SELECT 1 FROM departments WHERE guild_id = ?", (guild_id,)):
            for name, code, kind in DEFAULT_DEPARTMENTS:
                await self.db.execute(
                    "INSERT OR IGNORE INTO departments(guild_id, name, short_code, kind) VALUES (?, ?, ?, ?)",
                    (guild_id, name, code, kind),
                )
        if not await self.db.fetchone("SELECT 1 FROM status_codes WHERE guild_id = ?", (guild_id,)):
            for code, label, avail, sort in DEFAULT_STATUS_CODES:
                await self.db.execute(
                    "INSERT OR IGNORE INTO status_codes(guild_id, code, label, availability, sort) VALUES (?, ?, ?, ?, ?)",
                    (guild_id, code, label, avail, sort),
                )
            await self.db.set_config(guild_id, "status_defaults_version", 2)
        elif int(await self.db.get_config(guild_id, "status_defaults_version", 1) or 1) < 2:
            # v2 added 10-11 Traffic Stop. Added once; if an admin deletes it later it stays deleted.
            await self.db.execute(
                "INSERT OR IGNORE INTO status_codes(guild_id, code, label, availability, sort) VALUES (?, '10-11', 'Traffic Stop', 'assigned', 3)",
                (guild_id,))
            await self.db.set_config(guild_id, "status_defaults_version", 2)

    # ------------------------------------------------------------------ departments
    async def departments(self, guild_id: int, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM departments WHERE guild_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in await self.db.fetchall(sql, (guild_id,))]

    async def department_by_code(self, guild_id: int, code: str) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT * FROM departments WHERE guild_id = ? AND upper(short_code) = upper(?)", (guild_id, code)
        )
        return dict(row) if row else None

    async def add_department(self, guild_id: int, name: str, code: str, kind: str, actor_id: int, role_id: Optional[int] = None) -> dict:
        name = clean_text(name, 60, required=True, field="Department name")
        code = clean_text(code, 8, required=True, field="Short code").upper().replace(" ", "")
        if kind not in {"law", "fire", "ems", "supervisor", "dispatch", "other"}:
            raise ValidationError("Department type must be law, fire, ems, supervisor, dispatch or other.")
        existing = await self.department_by_code(guild_id, code)
        if existing:
            await self.db.execute("UPDATE departments SET name = ?, kind = ?, active = 1, role_id = COALESCE(?, role_id) WHERE id = ?",
                                  (name, kind, role_id, existing["id"]))
            dep_id = existing["id"]
        else:
            dep_id = await self.db.execute(
                "INSERT INTO departments(guild_id, name, short_code, kind, role_id) VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, code, kind, role_id),
            )
        await self.audit.record(guild_id, actor_id, "department.save", code, {"name": name, "kind": kind})
        await self.bus.publish(ev.CONFIG_CHANGED, guild_id, what="departments")
        return dict(await self.db.fetchone("SELECT * FROM departments WHERE id = ?", (dep_id,)))

    async def remove_department(self, guild_id: int, code: str, actor_id: int) -> bool:
        n = await self.db.execute_rowcount(
            "UPDATE departments SET active = 0 WHERE guild_id = ? AND upper(short_code) = upper(?)", (guild_id, code)
        )
        if n:
            await self.audit.record(guild_id, actor_id, "department.remove", code)
            await self.bus.publish(ev.CONFIG_CHANGED, guild_id, what="departments")
        return bool(n)

    # ------------------------------------------------------------------ status codes
    async def status_codes(self, guild_id: int) -> list[dict]:
        return [dict(r) for r in await self.db.fetchall(
            "SELECT * FROM status_codes WHERE guild_id = ? ORDER BY sort, code", (guild_id,))]

    async def status_code(self, guild_id: int, code: str) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT * FROM status_codes WHERE guild_id = ? AND upper(code) = upper(?)", (guild_id, code))
        return dict(row) if row else None

    async def save_status_code(self, guild_id: int, code: str, label: str, availability: str, actor_id: int) -> None:
        code = clean_text(code, 12, required=True, field="Code").upper()
        label = clean_text(label, 40, required=True, field="Label")
        if availability not in {"available", "assigned", "unavailable", "off"}:
            raise ValidationError("Availability must be available, assigned, unavailable or off.")
        sort = await self.db.fetchval("SELECT COALESCE(MAX(sort), 0) + 1 FROM status_codes WHERE guild_id = ?", (guild_id,), 1)
        await self.db.execute(
            "INSERT INTO status_codes(guild_id, code, label, availability, sort) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, code) DO UPDATE SET label = excluded.label, availability = excluded.availability",
            (guild_id, code, label, availability, sort),
        )
        await self.audit.record(guild_id, actor_id, "status_code.save", code, {"label": label, "availability": availability})

    async def remove_status_code(self, guild_id: int, code: str, actor_id: int) -> bool:
        in_use = await self.db.fetchone("SELECT 1 FROM units WHERE guild_id = ? AND upper(status_code) = upper(?)", (guild_id, code))
        if in_use:
            raise ValidationError("That status code is currently in use by a unit.")
        n = await self.db.execute_rowcount("DELETE FROM status_codes WHERE guild_id = ? AND upper(code) = upper(?)", (guild_id, code))
        if n:
            await self.audit.record(guild_id, actor_id, "status_code.remove", code)
        return bool(n)

    async def code_for_availability(self, guild_id: int, availability: str) -> Optional[str]:
        return await self.db.fetchval(
            "SELECT code FROM status_codes WHERE guild_id = ? AND availability = ? ORDER BY sort LIMIT 1", (guild_id, availability))

    # ------------------------------------------------------------------ officers & units
    async def officer_by_discord(self, guild_id: int, discord_id: int) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT o.*, u.callsign, u.status_code, u.current_incident_id, d.short_code AS dept_code, d.name AS dept_name "
            "FROM officers o LEFT JOIN units u ON u.id = o.unit_id LEFT JOIN departments d ON d.id = o.department_id "
            "WHERE o.guild_id = ? AND o.discord_id = ? AND o.active = 1",
            (guild_id, discord_id),
        )
        return dict(row) if row else None

    async def unit(self, unit_id: int) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT u.*, d.short_code AS dept_code, d.name AS dept_name, d.kind AS dept_kind, s.label AS status_label, s.availability "
            "FROM units u LEFT JOIN departments d ON d.id = u.department_id "
            "LEFT JOIN status_codes s ON s.guild_id = u.guild_id AND s.code = u.status_code WHERE u.id = ?",
            (unit_id,),
        )
        return dict(row) if row else None

    async def unit_by_callsign(self, guild_id: int, callsign: str) -> Optional[dict]:
        row = await self.db.fetchone("SELECT id FROM units WHERE guild_id = ? AND upper(callsign) = upper(?)", (guild_id, callsign.strip()))
        return await self.unit(row["id"]) if row else None

    async def unit_members(self, unit_id: int) -> list[dict]:
        return [dict(r) for r in await self.db.fetchall(
            "SELECT * FROM officers WHERE unit_id = ? AND active = 1 ORDER BY name", (unit_id,))]

    async def list_units(self, guild_id: int, include_off: bool = False) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT u.*, d.short_code AS dept_code, d.name AS dept_name, d.kind AS dept_kind, s.label AS status_label, s.availability, "
            "i.number AS incident_number, "
            "(SELECT group_concat(o.name, ', ') FROM officers o WHERE o.unit_id = u.id AND o.active = 1) AS members "
            "FROM units u LEFT JOIN departments d ON d.id = u.department_id "
            "LEFT JOIN status_codes s ON s.guild_id = u.guild_id AND s.code = u.status_code "
            "LEFT JOIN incidents i ON i.id = u.current_incident_id "
            "WHERE u.guild_id = ? ORDER BY d.id, u.callsign",
            (guild_id,),
        )
        units = [dict(r) for r in rows if (r["members"] or include_off)]
        if not include_off:
            units = [u for u in units if u.get("availability") != "off"]
        return units

    async def register(
        self,
        guild_id: int,
        discord_id: int,
        name: str,
        callsign: str,
        department_code: str,
        roblox_username: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> dict:
        name = clean_text(name, 40, required=True, field="Officer name")
        callsign = validate_callsign(callsign)
        dept = await self.department_by_code(guild_id, department_code)
        if dept is None or not dept["active"]:
            raise ValidationError(f"Department '{department_code}' is not configured on this server.")
        roblox_username = clean_text(roblox_username, 40) or None

        unit = await self.unit_by_callsign(guild_id, callsign)
        if unit is None:
            off_code = await self.code_for_availability(guild_id, "unavailable") or "10-7"
            unit_id = await self.db.execute(
                "INSERT INTO units(guild_id, callsign, department_id, status_code) VALUES (?, ?, ?, ?)",
                (guild_id, callsign, dept["id"], off_code),
            )
        else:
            unit_id = unit["id"]

        existing = await self.db.fetchone("SELECT * FROM officers WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
        if existing:
            await self.db.execute(
                "UPDATE officers SET name = ?, roblox_username = ?, department_id = ?, unit_id = ?, active = 1, updated_at = ? WHERE id = ?",
                (name, roblox_username, dept["id"], unit_id, iso_now(), existing["id"]),
            )
        else:
            await self.db.execute(
                "INSERT INTO officers(guild_id, discord_id, name, roblox_username, department_id, unit_id) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, discord_id, name, roblox_username, dept["id"], unit_id),
            )
        await self._cleanup_empty_units(guild_id)
        await self.audit.record(guild_id, actor_id or discord_id, "officer.register", callsign, {"discord_id": discord_id, "dept": dept["short_code"]})
        await self.bus.publish(ev.UNIT_UPDATED, guild_id, unit_id=unit_id)
        return await self.officer_by_discord(guild_id, discord_id)

    async def deactivate_officer(self, guild_id: int, discord_id: int, actor_id: int) -> bool:
        n = await self.db.execute_rowcount(
            "UPDATE officers SET active = 0, unit_id = NULL WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
        await self._cleanup_empty_units(guild_id)
        if n:
            await self.audit.record(guild_id, actor_id, "officer.remove", str(discord_id))
            await self.bus.publish(ev.UNIT_UPDATED, guild_id)
        return bool(n)

    async def _cleanup_empty_units(self, guild_id: int) -> None:
        # Units with no members and no active incident are removed to keep the board tidy.
        await self.db.execute(
            "DELETE FROM units WHERE guild_id = ? AND current_incident_id IS NULL AND id NOT IN "
            "(SELECT unit_id FROM officers WHERE unit_id IS NOT NULL AND active = 1) "
            "AND id NOT IN (SELECT unit_id FROM incident_assignments)",
            (guild_id,),
        )

    async def set_status(self, unit_id: int, code: str, actor_id: Optional[int], officer_id: Optional[int] = None) -> dict:
        unit = await self.unit(unit_id)
        if unit is None:
            raise UnitError("Unit not found")
        sc = await self.status_code(unit["guild_id"], code)
        if sc is None:
            raise UnitError(f"Status code '{code}' is not configured on this server.")
        if sc["code"] == unit["status_code"]:
            return unit
        await self.db.execute(
            "UPDATE units SET status_code = ?, status_changed_at = ? WHERE id = ?", (sc["code"], iso_now(), unit_id))
        await self.db.execute(
            "INSERT INTO unit_status_history(guild_id, unit_id, officer_id, old_code, new_code) VALUES (?, ?, ?, ?, ?)",
            (unit["guild_id"], unit_id, officer_id, unit["status_code"], sc["code"]),
        )
        await self.audit.record(unit["guild_id"], actor_id, "unit.status", unit["callsign"], {"old": unit["status_code"], "new": sc["code"]})
        await self.bus.publish(ev.UNIT_UPDATED, unit["guild_id"], unit_id=unit_id)
        await self.bus.publish(ev.ACTIVITY, unit["guild_id"], text=f"{unit['callsign']} is now {sc['code']} ({sc['label']})")
        return await self.unit(unit_id)

    async def availability_summary(self, guild_id: int) -> dict:
        units = await self.list_units(guild_id, include_off=True)
        summary = {"available": 0, "assigned": 0, "unavailable": 0, "off": 0, "by_department": {}}
        for u in units:
            if not u.get("members"):
                continue
            a = u.get("availability") or "unavailable"
            if u.get("current_incident_id"):
                a = "assigned"
            summary[a] = summary.get(a, 0) + 1
            dep = u.get("dept_code") or "?"
            d = summary["by_department"].setdefault(dep, {"available": 0, "assigned": 0, "unavailable": 0, "off": 0})
            d[a] = d.get(a, 0) + 1
        return summary

    async def available_units(self, guild_id: int, dept_kind: Optional[str] = None) -> list[dict]:
        units = await self.list_units(guild_id)
        return [u for u in units if u.get("availability") == "available" and not u.get("current_incident_id")
                and (dept_kind is None or u.get("dept_kind") == dept_kind)]
