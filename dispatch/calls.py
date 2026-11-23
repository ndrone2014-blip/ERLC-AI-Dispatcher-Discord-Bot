"""Automatic in-game 911 call intake from the ER:LC API.

Flow for every call in a /v2/server?EmergencyCalls=true snapshot:
  1. validate (CallNumber + StartedAt required for safe de-duplication)
  2. upsert into erlc_calls keyed by (guild, CallNumber, StartedAt)  -> duplicate prevention
  3. brand-new call -> create an API-VERIFIED incident and hand it to DispatchCore
  4. known call whose description/location/responders changed -> update + reassess
Calls are never discarded: invalid entries are counted and reported on the ER:LC panel.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from database.connection import Database
from database.models import dumps, iso_now, loads
from erlc.models import EmergencyCall, ServerSnapshot

if TYPE_CHECKING:
    from .core import DispatchCore

log = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    new_incidents: list[int] = field(default_factory=list)
    updated_incidents: list[int] = field(default_factory=list)
    ended_calls: int = 0
    invalid: int = 0


def describe_location(call: EmergencyCall, snapshot: Optional[ServerSnapshot]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Returns (location_text, postal, street, caller_name) using only API-provided data."""
    caller = snapshot.player_by_id(call.caller_id) if snapshot else None
    caller_name = caller.name if caller else None
    postal = street = None
    if caller and caller.location:
        postal = caller.location.postal
        street = caller.location.street
    location = call.position_descriptor
    if not location and (street or postal):
        # Fall back to the caller's *current* reported position (clearly labelled as such).
        bits = [b for b in [street, f"Postal {postal}" if postal else None] if b]
        location = "Caller's position: " + ", ".join(bits)
    return location, postal, street, caller_name


class CallIntake:
    def __init__(self, db: Database, core: "DispatchCore"):
        self.db = db
        self.core = core

    async def process_snapshot(self, guild_id: int, snapshot: ServerSnapshot, *, bootstrap: bool = False,
                               stale_after_s: int = 900) -> IntakeResult:
        result = IntakeResult(invalid=snapshot.invalid_calls)
        if snapshot.invalid_calls:
            log.warning("Guild %s: %s emergency call(s) lacked CallNumber/StartedAt and could not be tracked",
                        guild_id, snapshot.invalid_calls)
        seen_keys = set()
        for call in snapshot.emergency_calls:
            seen_keys.add((call.call_number, call.started_at))
            try:
                new_id, updated_id = await self._process_call(guild_id, call, snapshot, bootstrap=bootstrap, stale_after_s=stale_after_s)
            except Exception:
                log.exception("Failed to process ER:LC call %s for guild %s", call.call_number, guild_id)
                continue
            if new_id:
                result.new_incidents.append(new_id)
            if updated_id:
                result.updated_incidents.append(updated_id)
        if snapshot.emergency_calls_present:
            result.ended_calls = await self._mark_gone(guild_id, seen_keys)
        return result

    async def _process_call(self, guild_id: int, call: EmergencyCall, snapshot: ServerSnapshot, *, bootstrap: bool,
                            stale_after_s: int) -> tuple[Optional[int], Optional[int]]:
        location, postal, street, caller_name = describe_location(call, snapshot)
        existing = await self.db.fetchone(
            "SELECT * FROM erlc_calls WHERE guild_id = ? AND call_number = ? AND started_at = ?",
            (guild_id, call.call_number, call.started_at))
        players_json = dumps(sorted(call.responders))
        if existing is None:
            inserted = await self.db.execute_rowcount(
                "INSERT OR IGNORE INTO erlc_calls(guild_id, call_number, started_at, team, caller_id, caller_name, description, "
                "position_descriptor, position_x, position_z, players, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, call.call_number, call.started_at, call.team, call.caller_id, caller_name, call.description,
                 call.position_descriptor, call.position[0] if call.position else None, call.position[1] if call.position else None,
                 players_json, dumps(call.raw)),
            )
            check = await self.db.fetchone(
                "SELECT id, incident_id FROM erlc_calls WHERE guild_id = ? AND call_number = ? AND started_at = ?",
                (guild_id, call.call_number, call.started_at))
            if not inserted or check is None or check["incident_id"] is not None:
                return None, None  # another task won the race
            return await self._create_incident(guild_id, call, check["id"], location, postal, street, caller_name,
                                               bootstrap=bootstrap, stale_after_s=stale_after_s), None

        # Recovery: the call row exists but the incident was never created (e.g. crash mid-way). Never lose it.
        if existing["incident_id"] is None:
            await self.db.execute("UPDATE erlc_calls SET last_seen = ?, gone_at = NULL WHERE id = ?", (iso_now(), existing["id"]))
            return await self._create_incident(guild_id, call, existing["id"], location, postal, street, caller_name,
                                               bootstrap=bootstrap, stale_after_s=stale_after_s), None
        return await self._update_known(guild_id, call, existing, players_json)

    async def _create_incident(self, guild_id: int, call: EmergencyCall, call_row_id: int, location, postal, street, caller_name,
                               *, bootstrap: bool, stale_after_s: int) -> int:
        age = time.time() - call.started_at
        delayed = age > stale_after_s
        inc = await self.core.incidents.create(
            guild_id,
            source="erlc",
            verification="verified",
            nature=(call.description or None),
            description=call.description,
            location=location,
            postal=postal,
            street=street,
            position=call.position,
            caller_display=caller_name or (f"Roblox user {call.caller_id}" if call.caller_id else None),
            erlc_call_number=call.call_number,
            erlc_started_at=call.started_at,
            erlc_team=call.team,
        )
        await self.db.execute("UPDATE erlc_calls SET incident_id = ? WHERE id = ?", (inc["id"], call_row_id))
        await self.db.execute("UPDATE api_sync_state SET last_call_seen_at = ? WHERE guild_id = ?", (iso_now(), guild_id))
        await self.core.incidents.add_history(inc, "erlc_call", None, call_number=call.call_number, team=call.team,
                                              started_at=call.started_at, position=call.position,
                                              position_descriptor=call.position_descriptor, responders=call.responders)
        # Bootstrap = first sync for this server: old backlog calls are recorded and shown, but not spoken.
        speak = not (bootstrap and delayed)
        await self.core.handle_new_incident(inc["id"], speak=speak, delayed=delayed)
        return inc["id"]

    async def _update_known(self, guild_id: int, call: EmergencyCall, existing, players_json: str) -> tuple[Optional[int], Optional[int]]:
        await self.db.execute("UPDATE erlc_calls SET last_seen = ?, gone_at = NULL WHERE id = ?", (iso_now(), existing["id"]))
        incident_id = existing["incident_id"]
        changes = {}
        if (call.description or None) != (existing["description"] or None):
            changes["description"] = call.description
        if (call.position_descriptor or None) != (existing["position_descriptor"] or None):
            changes["position_descriptor"] = call.position_descriptor
        old_players = set(loads(existing["players"], []) or [])
        new_players = set(call.responders)
        if not changes and old_players == new_players:
            return None, None
        await self.db.execute(
            "UPDATE erlc_calls SET description = ?, position_descriptor = ?, players = ?, raw = ? WHERE id = ?",
            (call.description, call.position_descriptor, players_json, dumps(call.raw), existing["id"]))
        inc = await self.core.incidents.get(incident_id)
        if inc is None or inc["status"] == "closed":
            return None, None
        if new_players != old_players:
            await self.core.incidents.add_history(inc, "erlc_responders", None, responders=sorted(new_players),
                                                  note=f"{len(new_players)} in-game player(s) responding to the call")
            if new_players:
                await self.core.announcements.cancel_for_incident(incident_id, ("repeat",))
        if changes:
            fields = {}
            if "description" in changes:
                fields["description"] = changes["description"]
                fields["nature"] = changes["description"]
            if "position_descriptor" in changes and changes["position_descriptor"]:
                fields["location"] = changes["position_descriptor"]
            await self.core.incidents.update_fields(incident_id, None, **fields)
            await self.core.incidents.add_note(incident_id, None, f"In-game 911 call updated: {call.description or '(no description)'}",
                                               author="ER:LC API")
            await self.core.reassess(incident_id, reason="In-game call information changed")
        else:
            await self.core.bus.publish("incident.updated", guild_id, incident_id=incident_id, changes=["responders"])
        return None, incident_id

    async def _mark_gone(self, guild_id: int, seen_keys: set) -> int:
        rows = await self.db.fetchall(
            "SELECT id, call_number, started_at, incident_id FROM erlc_calls WHERE guild_id = ? AND gone_at IS NULL", (guild_id,))
        n = 0
        for r in rows:
            if (r["call_number"], r["started_at"]) in seen_keys:
                continue
            await self.db.execute("UPDATE erlc_calls SET gone_at = ? WHERE id = ?", (iso_now(), r["id"]))
            n += 1
            if r["incident_id"]:
                # The call left the in-game list. We do NOT auto-close the incident: a human decides.
                await self.core.incidents.add_history(r["incident_id"], "erlc_call_ended", None,
                                                      note="Call no longer listed by the ER:LC API (ended in-game)")
                inc = await self.core.incidents.get(r["incident_id"])
                if inc:
                    await self.core.announcements.cancel_for_incident(inc["id"], ("repeat",))
                    await self.core.bus.publish("incident.updated", guild_id, incident_id=inc["id"], changes=["erlc_ended"])
        return n
