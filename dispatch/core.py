"""DispatchCore: wires services together and owns the incident pipeline.

new incident -> assess (AI, falls back to rules) -> store recommendation ->
build announcement (AI wording only if facts preserved, else template) ->
enqueue (DB-backed, deduplicated, prioritised) -> panels/voice react via events.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ai.assessment import announcement_for_new_call, assess_incident, escalation_text
from ai.manager import AIManager
from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import PRIORITY_LABEL, PRIORITY_RANK, loads, parse_iso, utcnow
from security.audit import AuditLog

from .announcements import AnnouncementService
from .assignments import AssignmentService
from .bolo import BoloService, bolo_spoken
from .calls import CallIntake
from .escalation import compare_assessments
from .incidents import IncidentService
from .reports import ReportService
from .units import UnitService

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "priority_approval_threshold": "high",   # priorities >= this need supervisor approval
    "repeat_enabled": True,
    "repeat_interval_s": 120,
    "repeat_max": 2,
    "repeat_min_priority": "medium",
    "ai_announcement_wording": True,
    "assessment_timeout_s": 8,
    "stale_call_s": 900,
    "allow_unverified_reports": True,
}


class DispatchCore:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog, ai: Optional[AIManager]):
        self.db = db
        self.bus = bus
        self.audit = audit
        self.ai = ai
        self.incidents = IncidentService(db, bus, audit)
        self.units = UnitService(db, bus, audit)
        self.assignments = AssignmentService(db, bus, audit, self.incidents, self.units)
        self.announcements = AnnouncementService(db, bus)
        self.bolos = BoloService(db, bus, audit)
        self.reports = ReportService(db, bus, audit)
        self.intake = CallIntake(db, self)
        self._reassess_locks: dict[int, asyncio.Lock] = {}
        bus.subscribe(ev.ASSIGNMENT_CHANGED, self._on_assignment)
        bus.subscribe(ev.BACKUP_REQUESTED, self._on_backup)
        bus.subscribe(ev.INCIDENT_CLOSED, self._on_closed)
        bus.subscribe(ev.BOLO_CHANGED, self._on_bolo)
        bus.subscribe(ev.REPORT_CHANGED, self._on_report)

    async def setting(self, guild_id: int, key: str):
        settings = await self.db.get_config(guild_id, "dispatch_settings", {}) or {}
        return settings.get(key, DEFAULT_SETTINGS[key])

    async def set_setting(self, guild_id: int, key: str, value) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        settings = await self.db.get_config(guild_id, "dispatch_settings", {}) or {}
        settings[key] = value
        await self.db.set_config(guild_id, "dispatch_settings", settings)
        await self.bus.publish(ev.CONFIG_CHANGED, guild_id, what="dispatch_settings")

    # ------------------------------------------------------------------ assessment
    async def _assess(self, inc: dict) -> dict:
        guild_id = inc["guild_id"]
        departments = await self.units.departments(guild_id)
        assigned = await self.incidents.assigned_units(inc["id"])
        availability = await self.units.availability_summary(guild_id)
        notes = [n["detail"].get("note", "") for n in await self.incidents.notes(inc["id"])]
        timeout = float(await self.setting(guild_id, "assessment_timeout_s"))
        try:
            return await asyncio.wait_for(
                assess_incident(self.ai, guild_id, inc, departments=departments, assigned_units=assigned,
                                availability=availability, notes=notes), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("AI assessment for incident #%s timed out after %ss; using rule-based assessment", inc["number"], timeout)
            return await assess_incident(None, guild_id, inc, departments=departments, assigned_units=assigned,
                                         availability=availability, notes=notes)

    async def handle_new_incident(self, incident_id: int, *, speak: bool = True, delayed: bool = False,
                                  skip_reason: str = "Backlog call found on first sync; recorded and shown without voice announcement") -> dict:
        inc = await self.incidents.get(incident_id)
        assessment = await self._assess(inc)
        threshold = await self.setting(inc["guild_id"], "priority_approval_threshold")
        _, inc = await self.incidents.apply_assessment(incident_id, assessment, approval_threshold=threshold, reason="initial assessment")
        if speak:
            use_ai = bool(await self.setting(inc["guild_id"], "ai_announcement_wording"))
            try:
                text, wording_source = await asyncio.wait_for(
                    announcement_for_new_call(self.ai, inc["guild_id"], inc, use_ai=use_ai), timeout=5)
            except asyncio.TimeoutError:
                from ai.assessment import template_new_call
                text, wording_source = template_new_call(inc), "template"
            if delayed:
                text = "Delayed call notice. " + text
            await self.announcements.enqueue(inc["guild_id"], "new_911", text, dedupe_key=f"new911:{incident_id}",
                                             incident_id=incident_id, incident_priority=inc.get("priority"))
            await self.incidents.add_history(inc, "announcement_queued", None, wording=wording_source)
        else:
            await self.incidents.add_history(inc, "announcement_skipped", None, reason=skip_reason)
        await self.bus.publish(ev.INCIDENT_UPDATED, inc["guild_id"], incident_id=incident_id, changes=["assessment"])
        return inc

    async def reassess(self, incident_id: int, reason: str = "") -> Optional[dict]:
        lock = self._reassess_locks.setdefault(incident_id, asyncio.Lock())
        async with lock:
            inc = await self.incidents.get(incident_id)
            if inc is None or inc["status"] == "closed":
                return None
            assessment = await self._assess(inc)
            threshold = await self.setting(inc["guild_id"], "priority_approval_threshold")
            old, new = await self.incidents.apply_assessment(incident_id, assessment, approval_threshold=threshold, reason=reason)
            change = compare_assessments(old, new)
            if change:
                await self.incidents.add_history(new, "escalation", None, **change)
                key = f"esc:{incident_id}:{new.get('ai_priority')}:{new.get('recommended_units')}:{','.join(loads(new.get('recommended_departments'), []) or [])}"
                await self.announcements.enqueue(new["guild_id"], "escalation", escalation_text(new, change), dedupe_key=key,
                                                 incident_id=incident_id, incident_priority=new.get("ai_priority"))
                await self.bus.publish(ev.ACTIVITY, new["guild_id"],
                                       text=f"Incident #{new['number']} reassessed: " + "; ".join(change["reasons"]))
            await self.bus.publish(ev.INCIDENT_UPDATED, new["guild_id"], incident_id=incident_id, changes=["assessment"])
            return change

    # ------------------------------------------------------------------ manual / discord / test incidents
    async def create_discord_call(self, guild_id: int, user_id: int, *, caller: str, location: str, category: str,
                                  description: str, requested_department: str = "", extra: str = "") -> dict:
        inc = await self.incidents.create(
            guild_id, source="discord", verification="unverified", nature=category or description[:80],
            description=description + (f"\nAdditional info: {extra}" if extra else ""), location=location or None,
            caller_display=caller or "Anonymous", category=category or None, requested_department=requested_department or None,
            created_by=user_id)
        return await self.handle_new_incident(inc["id"])

    async def create_manual_incident(self, guild_id: int, user_id: int, *, nature: str, location: str, description: str = "",
                                     reporter: str = "") -> dict:
        inc = await self.incidents.create(
            guild_id, source="manual", verification="unverified", nature=nature, description=description or None,
            location=location or None, caller_display=f"Officer report: {reporter}" if reporter else "Officer report",
            created_by=user_id)
        return await self.handle_new_incident(inc["id"])

    SELF_INITIATED_LABELS = {
        "traffic_stop": ("Traffic stop", "Traffic stop", "10-11"),
        "subject_stop": ("Subject stop", "Subject stop", "10-23"),
        "pursuit": ("Vehicle pursuit", "Pursuit / fleeing subject", "10-23"),
        "foot_pursuit": ("Foot pursuit", "Pursuit / fleeing subject", "10-23"),
    }

    async def create_officer_initiated(self, guild_id: int, actor_id: int, unit_id: int, *, kind: str,
                                       location: Optional[str] = None, plate: Optional[str] = None,
                                       detail: str = "") -> tuple[dict, dict]:
        """An officer starts their own activity (traffic stop, pursuit...). Returns (incident, unit).

        Recorded as an officer-initiated, UNVERIFIED incident (the ER:LC API does not expose stops/pursuits).
        No new-911 broadcast: the dispatcher's spoken read-back is the radio acknowledgement.
        Pursuits additionally broadcast an urgent backup request to all units.
        """
        if kind not in self.SELF_INITIATED_LABELS:
            raise ValueError(f"Unknown activity {kind}")
        nature, category, status_code = self.SELF_INITIATED_LABELS[kind]
        unit = await self.units.unit(unit_id)
        description = " · ".join(x for x in [f"Plate {plate}" if plate else None, detail or None] if x) or None
        inc = await self.incidents.create(
            guild_id, source="manual", verification="unverified", nature=nature + (f" — {plate}" if plate else ""),
            description=description, location=location, category=category,
            caller_display=f"Officer-initiated: {unit['callsign'] if unit else 'unit'}", created_by=actor_id)
        await self.incidents.add_history(inc, "officer_initiated", actor_id, kind=kind, plate=plate)
        await self.handle_new_incident(inc["id"], speak=False, skip_reason="Officer-initiated; acknowledged by voice read-back")
        inc, unit, _ = await self.assignments.attach(inc["id"], unit_id, actor_id)
        code = status_code if await self.units.status_code(guild_id, status_code) else (
            await self.units.code_for_availability(guild_id, "assigned"))
        if code:
            unit = await self.units.set_status(unit_id, code, actor_id)
        if kind in {"pursuit", "foot_pursuit"}:
            await self.assignments.request_backup(guild_id, unit_id, inc["id"], actor_id, "urgent",
                                                  f"{nature}{' near ' + location if location else ''}")
        return await self.incidents.get(inc["id"]), unit

    async def create_test_call(self, guild_id: int, user_id: int, *, location: str, description: str) -> dict:
        inc = await self.incidents.create(
            guild_id, source="test", verification="test", nature=f"[TEST DATA] {description}",
            description=f"SIMULATED TEST CALL — not a real ER:LC call. {description}", location=location or None,
            caller_display="TEST DATA", created_by=user_id)
        await self.audit.record(guild_id, user_id, "test.call", f"incident:{inc['number']}")
        return await self.handle_new_incident(inc["id"])

    # ------------------------------------------------------------------ repeat announcements
    async def repeat_tick(self, guild_id: int) -> int:
        if not await self.setting(guild_id, "repeat_enabled"):
            return 0
        interval = int(await self.setting(guild_id, "repeat_interval_s"))
        max_repeats = int(await self.setting(guild_id, "repeat_max"))
        min_prio = await self.setting(guild_id, "repeat_min_priority")
        queued = 0
        for inc in await self.incidents.list_active(guild_id):
            if inc["status"] != "pending":
                continue  # a unit is attached
            if PRIORITY_RANK.get(inc.get("priority") or "routine", 1) < PRIORITY_RANK.get(min_prio, 2):
                continue
            if inc["source"] == "erlc":
                row = await self.db.fetchone("SELECT players, gone_at FROM erlc_calls WHERE incident_id = ?", (inc["id"],))
                if row and (loads(row["players"], []) or row["gone_at"]):
                    continue  # in-game units responded or call ended
            plays, last = await self.announcements.play_stats(inc["id"])
            if plays == 0 or plays > max_repeats:
                continue
            last_dt = parse_iso(last)
            if last_dt is None or (utcnow() - last_dt).total_seconds() < interval:
                continue
            if await self.announcements.has_queued(inc["id"], ("new_911", "repeat")):
                continue
            n = plays  # repeat number
            text = (f"Dispatch repeating, incident {inc['number']} is still awaiting a unit. "
                    f"{PRIORITY_LABEL.get(inc.get('priority') or '', '')} priority. Location: {inc.get('location') or 'unavailable'}. "
                    f"Nature: {inc.get('nature') or 'not provided'}. Any available unit, please respond.")
            if inc["source"] == "test":
                text = "Test data. " + text
            if await self.announcements.enqueue(guild_id, "repeat", text, dedupe_key=f"repeat:{inc['id']}:{n}",
                                                incident_id=inc["id"], incident_priority=inc.get("priority")):
                queued += 1
        return queued

    # ------------------------------------------------------------------ event-driven announcements
    async def _on_assignment(self, e: ev.Event) -> None:
        if e.data.get("action") != "attach":
            return
        inc = await self.incidents.get(e.data["incident_id"])
        unit = await self.units.unit(e.data["unit_id"])
        if not inc or not unit:
            return
        actor = e.data.get("actor_id")
        if actor and await self.db.fetchone("SELECT 1 FROM officers WHERE unit_id = ? AND discord_id = ? AND active = 1",
                                            (unit["id"], actor)):
            return  # officer attached themselves; their own read-back already acknowledges it
        await self.announcements.enqueue(
            e.guild_id, "unit_assignment", f"{unit['callsign']}, show you attached to incident {inc['number']}, {inc.get('location') or 'location unavailable'}.",
            dedupe_key=f"assign:{inc['id']}:{unit['id']}:{utcnow().strftime('%H%M%S')}", incident_id=inc["id"])

    async def _on_backup(self, e: ev.Event) -> None:
        unit = await self.units.unit(e.data["unit_id"]) if e.data.get("unit_id") else None
        inc = await self.incidents.get(e.data["incident_id"]) if e.data.get("incident_id") else None
        who = unit["callsign"] if unit else "A unit"
        where = f" on incident {inc['number']}, {inc.get('location') or 'location unavailable'}" if inc else ""
        urgency = e.data.get("urgency", "routine")
        lead = "Priority traffic. " if urgency == "emergency" else ""
        text = f"{lead}{who} is requesting {'emergency ' if urgency == 'emergency' else ''}backup{where}. Available units respond."
        await self.announcements.enqueue(e.guild_id, "backup_request", text, dedupe_key=f"backup:{e.data['request_id']}",
                                         incident_id=inc["id"] if inc else None,
                                         critical=urgency == "emergency", priority=95 if urgency == "emergency" else None)
        if inc:
            await self.reassess(inc["id"], reason="Backup requested")

    async def _on_closed(self, e: ev.Event) -> None:
        inc = await self.incidents.get(e.data["incident_id"])
        if inc:
            await self.announcements.enqueue(e.guild_id, "incident_closure", f"Incident {inc['number']} is now closed.",
                                             dedupe_key=f"close:{inc['id']}", incident_id=inc["id"])

    async def _on_bolo(self, e: ev.Event) -> None:
        if e.data.get("action") != "created":
            return
        b = await self.bolos.get(e.data["bolo_id"])
        if b:
            await self.announcements.enqueue(e.guild_id, "bolo", bolo_spoken(b), dedupe_key=f"bolo:{b['id']}")

    async def _on_report(self, e: ev.Event) -> None:
        if e.data.get("action") != "created":
            return
        rep = await self.reports.get(e.data["report_id"])
        if rep:
            # Never read private report contents aloud - only that one exists.
            await self.announcements.enqueue(e.guild_id, "new_report",
                                             f"Dispatch advises, new police report number {rep['number']} has been filed and is awaiting review.",
                                             dedupe_key=f"report:{rep['id']}")

    async def department_notice(self, guild_id: int, actor_id: int, text: str) -> Optional[int]:
        await self.audit.record(guild_id, actor_id, "notice.department", None, {"text": text[:200]})
        return await self.announcements.enqueue(guild_id, "department_notice", text,
                                                dedupe_key=f"notice:{actor_id}:{utcnow().timestamp():.0f}")

    # ------------------------------------------------------------------ situation awareness
    async def situation_text(self, guild_id: int) -> str:
        lines = ["ACTIVE INCIDENTS:"]
        for inc in await self.incidents.list_active(guild_id):
            units = await self.incidents.assigned_units(inc["id"])
            lines.append(
                f"- #{inc['number']} [{inc['verification']}] {PRIORITY_LABEL.get(inc.get('priority') or '', 'Unassessed')} "
                f"({inc['priority_status']}) | {inc.get('nature') or 'unknown'} | {inc.get('location') or 'location unavailable'} | "
                f"status {inc['status']} | units: {', '.join(u['callsign'] for u in units) or 'none'} | "
                f"recommended {inc.get('recommended_units') or '?'} units {loads(inc.get('recommended_departments'), [])}")
        if len(lines) == 1:
            lines.append("- none")
        lines.append("UNITS:")
        for u in await self.units.list_units(guild_id):
            lines.append(f"- {u['callsign']} ({u.get('dept_code')}) {u['status_code']} {u.get('status_label') or ''}"
                         f"{' on #' + str(u['incident_number']) if u.get('incident_number') else ''} — {u.get('members') or ''}")
        backups = await self.assignments.open_backup_requests(guild_id)
        lines.append("OPEN BACKUP REQUESTS: " + (", ".join(f"{b.get('callsign') or '?'} on #{b.get('incident_number') or '-'} ({b['urgency']})" for b in backups) or "none"))
        bolos = await self.bolos.active(guild_id)
        lines.append("ACTIVE BOLOS: " + ("; ".join(f"#{b['number']} {b['kind']}: {b['subject']}" for b in bolos) or "none"))
        return "\n".join(lines)
