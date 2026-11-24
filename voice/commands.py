"""Officer requests (voice or text) -> intents -> database actions -> spoken/text reply.

The reply is only produced AFTER the database confirms the action, so the
dispatcher never claims an assignment that did not happen.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from ai.conversation import ConversationMemory, Intent, answer_question, parse_rules, parse_with_ai, strip_wake_word
from ai.manager import AIUnavailable
from database.models import PRIORITY_LABEL, loads
from dispatch.incidents import IncidentError
from dispatch.units import UnitError

from .audio_processing import pcm48k_stereo_to_16k_mono, rms

if TYPE_CHECKING:
    from dispatch.core import DispatchCore
    from security.permissions import PermissionManager

    from .connection import VoiceManager
    from .speech_to_text import STTEngine

log = logging.getLogger(__name__)


async def latest_call(core: "DispatchCore", guild_id: int, unit_id: Optional[int]) -> tuple[Optional[dict], str]:
    """Which call does an officer mean by "attach me to the call"? Decided from the database:
    1. the only active incident, else
    2. the incident dispatch most recently announced (new call / repeat) in the last 15 minutes,
       that the unit is not already on, else
    3. ask which incident number (listing the open ones)."""
    active = [i for i in await core.incidents.list_active(guild_id)]
    if unit_id:
        u = await core.units.unit(unit_id)
        current = u.get("current_incident_id") if u else None
        active = [i for i in active if i["id"] != current] or active
    if not active:
        return None, "Dispatch has no active calls to attach you to."
    if len(active) == 1:
        return active[0], ""
    row = await core.db.fetchone(
        "SELECT a.incident_id FROM announcements a JOIN incidents i ON i.id = a.incident_id "
        "WHERE a.guild_id = ? AND a.kind IN ('new_911','repeat') AND a.status IN ('played','muted') "
        "AND a.played_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-15 minutes') AND i.status != 'closed' "
        "ORDER BY a.played_at DESC LIMIT 1", (guild_id,))
    if row:
        for i in active:
            if i["id"] == row["incident_id"]:
                return i, ""
    numbers = ", ".join(str(i["number"]) for i in active[:6])
    return None, f"Which incident? Active calls are {numbers}. Say, Dispatch, attach me to incident {active[0]['number']}."


async def execute_intent(core: "DispatchCore", guild_id: int, officer: Optional[dict], intent: Intent,
                         actor_id: int, memory: Optional[ConversationMemory] = None) -> str:
    """Run an intent against the authoritative database and return the dispatcher's reply."""
    if intent.clarification:
        return intent.clarification
    name, args = intent.name, intent.args
    callsign = officer.get("callsign") if officer else None
    unit_id = officer.get("unit_id") if officer else None

    async def resolve_incident(required: bool = True) -> Optional[dict]:
        num = args.get("incident_number")
        if num is not None:
            inc = await core.incidents.get_by_number(guild_id, int(num))
            if inc is None:
                raise IncidentError(f"Dispatch has no incident number {num}.")
            return inc
        if unit_id:
            u = await core.units.unit(unit_id)
            if u and u.get("current_incident_id"):
                return await core.incidents.get(u["current_incident_id"])
        if required:
            raise IncidentError("Which incident number?")
        return None

    try:
        if name in {"status_change", "attach", "detach", "backup", "add_note"} and not unit_id:
            return "Unidentified unit. State your callsign and register with slash unit register before using voice commands."
        if name == "status_change":
            unit = await core.assignments.set_unit_status(unit_id, args["code"], actor_id, officer["id"])
            return f"{callsign}, copy, showing you {unit['status_code']}, {unit.get('status_label') or ''}.".replace(", .", ".")
        if name == "attach":
            if args.get("incident_number") is None:
                inc, question = await latest_call(core, guild_id, unit_id)
                if inc is None:
                    return question
            else:
                inc = await resolve_incident()
            inc, unit, prev = await core.assignments.attach(inc["id"], unit_id, actor_id)
            extra = f" Cleared you from incident {prev['number']}." if prev else ""
            return f"{callsign}, copy, attached to incident {inc['number']}, {inc.get('location') or 'location unavailable'}.{extra}"
        if name == "detach":
            inc = await resolve_incident()
            await core.assignments.detach(inc["id"], unit_id, actor_id)
            return f"{callsign}, copy, clear from incident {inc['number']}."
        if name == "backup":
            req = await core.assignments.request_backup(guild_id, unit_id, None, actor_id, args.get("urgency", "routine"),
                                                        args.get("note", ""))
            if req.get("duplicate"):
                return f"{callsign}, your backup request is already out."
            return f"{callsign}, copy, backup request sent."
        if name == "add_note":
            inc = await resolve_incident()
            text = str(args.get("text") or "").strip()
            if not text:
                return "Copy, what is the update?"
            await core.incidents.add_note(inc["id"], actor_id, text, author=callsign or "")
            asyncio.create_task(core.reassess(inc["id"], reason=f"Update from {callsign}"))
            return f"{callsign}, copy update on incident {inc['number']}. Dispatch is reassessing."
        if name == "query_highest":
            inc = await core.incidents.highest_priority(guild_id)
            if inc is None:
                return "Dispatch has no active calls."
            units = await core.incidents.assigned_units(inc["id"])
            return (f"Highest priority is incident {inc['number']}, {PRIORITY_LABEL.get(inc.get('priority') or '', 'unassessed')} priority, "
                    f"{inc.get('nature') or 'nature unknown'}, at {inc.get('location') or 'location unavailable'}. "
                    f"{len(units)} unit{'s' if len(units) != 1 else ''} assigned.")
        if name == "query_active":
            active = await core.incidents.list_active(guild_id)
            if not active:
                return "Dispatch has no active calls."
            parts = [f"{i['number']}, {PRIORITY_LABEL.get(i.get('priority') or '', 'unassessed')}, {i.get('nature') or 'unknown'}"
                     for i in active[:5]]
            more = f" and {len(active) - 5} more" if len(active) > 5 else ""
            return f"{len(active)} active call{'s' if len(active) != 1 else ''}: " + "; ".join(parts) + more + "."
        if name == "query_incident_units":
            inc = await resolve_incident()
            units = await core.incidents.assigned_units(inc["id"])
            rec = inc.get("recommended_units")
            names = ", ".join(u["callsign"] for u in units) or "none"
            return (f"Incident {inc['number']} has {len(units)} unit{'s' if len(units) != 1 else ''} assigned: {names}."
                    + (f" Recommended is {rec}." if rec else ""))
        if name == "query_incident":
            inc = await resolve_incident()
            deps = ", ".join(loads(inc.get("recommended_departments"), []) or [])
            return (f"Incident {inc['number']}: {inc.get('nature') or 'nature unknown'} at {inc.get('location') or 'location unavailable'}, "
                    f"{PRIORITY_LABEL.get(inc.get('priority') or '', 'unassessed')} priority, status {inc['status']}."
                    + (f" Recommended resources {deps}." if deps else ""))
        if name == "query_available":
            avail = await core.units.available_units(guild_id)
            if not avail:
                return "No units are currently showing available."
            return f"{len(avail)} available: " + ", ".join(u["callsign"] for u in avail[:8]) + "."
        if name == "query_bolos":
            bolos = await core.bolos.active(guild_id)
            if not bolos:
                return "No active BOLOs."
            return "Active BOLOs: " + "; ".join(f"number {b['number']}, {b['subject']}" for b in bolos[:4]) + "."
        if name == "question" and core.ai is not None:
            situation = await core.situation_text(guild_id)
            ctx = memory.get(guild_id, actor_id) if memory else []
            return await answer_question(core.ai, guild_id, str(args.get("text") or ""), situation, ctx)
        return "Dispatch, say again? I didn't understand the request."
    except (IncidentError, UnitError) as e:
        return str(e)
    except AIUnavailable:
        return "Dispatch can't answer that right now. Check the Discord panels."


class VoiceCommandProcessor:
    def __init__(self, core: "DispatchCore", voice: "VoiceManager", stt: "STTEngine", permissions: "PermissionManager",
                 log_callback: Callable[[int, str, str, str], Awaitable[None]], member_lookup):
        self.core = core
        self.voice = voice
        self.stt = stt
        self.permissions = permissions
        self.log_callback = log_callback      # (guild_id, speaker_label, transcript, reply)
        self.member_lookup = member_lookup    # (guild_id, user_id) -> Member | None
        self.memory = ConversationMemory()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._task: Optional[asyncio.Task] = None
        self._recent: dict[tuple, float] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="voice-commands")

    def submit(self, guild_id: int, user_id: int, pcm: bytes) -> None:
        try:
            self._queue.put_nowait((guild_id, user_id, pcm))
        except asyncio.QueueFull:
            log.warning("Voice command queue full; dropping an utterance from %s", user_id)

    async def _worker(self) -> None:
        while True:
            guild_id, user_id, pcm = await self._queue.get()
            try:
                await self._handle(guild_id, user_id, pcm)
            except Exception:
                log.exception("Voice command failed")

    async def _handle(self, guild_id: int, user_id: int, pcm: bytes) -> None:
        settings = await self.voice.settings(guild_id)
        if not settings["listening"]:
            return
        audio = pcm48k_stereo_to_16k_mono(pcm)
        if rms(audio) < 0.004:
            return  # near-silence
        text = await self.stt.transcribe(audio)
        if not text or len(text) < 3:
            return
        woke, rest = strip_wake_word(text)
        if settings["require_wake_word"] and not woke:
            return
        member = self.member_lookup(guild_id, user_id)
        if member is None or getattr(member, "bot", False):
            return
        from security.permissions import Actor
        if not await self.permissions.has(guild_id, Actor.from_member(member), "voice.command"):
            await self._reply(guild_id, member.display_name, text,
                              "Dispatch only accepts radio traffic from registered personnel.")
            return
        officer = await self.core.units.officer_by_discord(guild_id, user_id)
        key = (guild_id, user_id, rest.lower())
        now = time.monotonic()
        if now - self._recent.get(key, 0) < 4:
            return  # same transmission picked up twice
        self._recent[key] = now
        speaker = officer["callsign"] if officer and officer.get("callsign") else member.display_name
        statuses = await self.core.units.status_codes(guild_id)
        intent = parse_rules(rest, statuses)
        if intent is None and self.core.ai is not None:
            ctx = self.memory.get(guild_id, user_id)
            intent = await parse_with_ai(self.core.ai, guild_id, rest, statuses, ctx,
                                         officer.get("current_incident_id") if officer else None)
            if intent.name == "question":
                intent.args.setdefault("text", rest)
        if intent is None:
            intent = Intent("unclear", clarification="Dispatch, say again?")
        if officer is None and intent.name not in {"query_highest", "query_active", "query_available", "query_bolos", "question"}:
            reply = "Unidentified station, state your callsign. Register your unit with slash unit register."
        else:
            reply = await execute_intent(self.core, guild_id, officer, intent, user_id, self.memory)
        self.memory.add(guild_id, user_id, speaker, rest)
        self.memory.add(guild_id, user_id, "Dispatch", reply)
        await self._reply(guild_id, speaker, text, reply)

    async def _reply(self, guild_id: int, speaker: str, transcript: str, reply: str) -> None:
        await self.core.announcements.enqueue(guild_id, "voice_reply", reply,
                                              dedupe_key=f"reply:{time.time():.3f}", priority=75)
        await self.log_callback(guild_id, speaker, transcript, reply)
