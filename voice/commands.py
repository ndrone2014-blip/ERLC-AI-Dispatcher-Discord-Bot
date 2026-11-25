"""Officer requests (voice or text) -> intents -> database actions -> spoken/text reply.

The reply is only produced AFTER the database confirms the action, so the
dispatcher never claims an assignment that did not happen.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from ai.conversation import (ConversationMemory, Intent, answer_question, maybe_wake_word, parse_rules, parse_with_ai,
                             strip_wake_word)
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
                         actor_id: int, memory: Optional[ConversationMemory] = None, caller: Optional[str] = None) -> str:
    """Run an intent against the authoritative database and return the dispatcher's reply."""
    if intent.clarification:
        return intent.clarification
    if intent.name == "report_incident":
        return await execute_report(core, guild_id, officer, intent, actor_id, memory, caller)
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
        if name in {"status_change", "attach", "detach", "backup", "add_note", "self_initiated"} and not unit_id:
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
        if name == "self_initiated":
            kind = str(args.get("kind") or "traffic_stop")
            if kind not in core.SELF_INITIATED_LABELS:
                return "Copy. What type of stop? Say traffic stop, subject stop, or pursuit."
            location = (str(args["location"]).strip() or None) if args.get("location") else None
            plate = (str(args["plate"]).strip().upper() or None) if args.get("plate") else None
            inc, unit = await core.create_officer_initiated(guild_id, actor_id, unit_id, kind=kind, location=location,
                                                            plate=plate, detail=str(args.get("detail") or "")[:200])
            label = core.SELF_INITIATED_LABELS[kind][0].lower()
            where = f" at {location}" if location else ""
            plate_txt = f", plate {' '.join(plate)}" if plate else ""
            if kind in {"pursuit", "foot_pursuit"}:
                return (f"{callsign}, copy, {label}{where}{plate_txt}. Incident {inc['number']}. "
                        f"All units be advised, backup requested.")
            ask = "" if location else " Advise your location."
            return f"{callsign}, copy, showing you on a {label}{where}{plate_txt}. Incident {inc['number']}.{ask}"
        if name == "detach":
            inc = await resolve_incident()
            await core.assignments.detach(inc["id"], unit_id, actor_id)
            # An officer-initiated stop with nobody left on it is finished when the officer clears.
            if (inc["source"] == "manual" and (inc.get("caller_display") or "").startswith("Officer-initiated")
                    and not await core.incidents.assigned_units(inc["id"])):
                await core.incidents.close(inc["id"], actor_id, f"Cleared by {callsign}")
                return f"{callsign}, copy, clear. Incident {inc['number']} closed."
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
        if name == "radio_check":
            return f"{callsign + ', ' if callsign else ''}dispatch copies loud and clear, go ahead."
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


async def execute_report(core: "DispatchCore", guild_id: int, officer: Optional[dict], intent: Intent, actor_id: int,
                         memory: Optional[ConversationMemory], caller: Optional[str]) -> str:
    """A natural-language incident report: ask for what is missing, merge duplicates, otherwise open an incident."""
    from dispatch import phrases
    args = intent.args
    text = str(args.get("text") or "")
    missing = list(args.get("missing") or [])
    ask = bool(await core.setting(guild_id, "follow_up_questions"))
    if missing and ask and memory is not None and not args.get("asked_twice"):
        from dispatch import nlu
        a = nlu.analyze(text)
        data = {"text": text, "nature": args.get("nature"), "category": args.get("category"), "location": args.get("location"),
                "asked_twice": bool(args.get("follow_up"))}
        memory.set_pending(guild_id, actor_id, data)
        return a.follow_up() or phrases.ask_again(guild_id)
    if not args.get("nature") and not args.get("location"):
        return phrases.ask_again(guild_id)
    who = caller or (officer.get("callsign") if officer else None) or "Discord caller"
    inc, merged = await core.report_incident(guild_id, actor_id, text=text, caller=who, location=args.get("location"),
                                             category=args.get("category"), nature=args.get("nature"))
    if memory is not None:
        memory.last_report[(guild_id, actor_id)] = (inc, merged)
    await core.bus.publish("dispatch.report", guild_id, incident_id=inc["id"], merged=merged, text=text[:500],
                           actor_id=actor_id, source=intent.source)
    if merged:
        return phrases.duplicate_reply(inc, guild_id=guild_id)
    style = await core.setting(guild_id, "response_style")
    return phrases.call_received_reply(inc, style=style, guild_id=guild_id)


_WORD = re.compile(r"[a-z0-9]+")


def looks_like_echo(heard: str, spoken: str) -> bool:
    """True when a transcript is (mostly) the dispatcher's own last transmission picked up by
    someone's microphone. Officers talking over the dispatcher say different words, so they pass."""
    h = _WORD.findall((heard or "").lower())
    sp = _WORD.findall((spoken or "").lower())
    if len(h) < 2 or not sp:
        return False
    spoken_set = set(sp)
    overlap = sum(1 for w in h if w in spoken_set) / len(h)
    return overlap >= 0.7 or SequenceMatcher(None, " ".join(h), " ".join(sp)).ratio() >= 0.75


class VoiceCommandProcessor:
    STALE_AFTER_S = 10.0    # a clip waiting longer than this is outdated - drop it instead of answering late
    QUEUE_MAX = 60          # clips waiting for speech-to-text (all speakers together)
    PER_USER_MAX = 3        # clips waiting per speaker - one person talking non-stop can't block everyone else
    SCREEN_S = 2.5          # the wake word is at the start, so chatter is screened on its first seconds only
    ECHO_WINDOW_S = 1.5     # clips ending this soon after the dispatcher stopped talking are checked for echo
    OFFICER_CACHE_S = 30.0
    AWAKE_S = 6.0           # after "Dispatch" alone, the speaker's next transmission needs no wake word
    GO_AHEAD_DELAY_S = 1.2  # ... and if they don't continue, dispatch answers "go ahead"

    def __init__(self, core: "DispatchCore", voice: "VoiceManager", stt: "STTEngine", permissions: "PermissionManager",
                 log_callback: Callable[[int, str, str, str], Awaitable[None]], member_lookup):
        self.core = core
        self.voice = voice
        self.stt = stt
        self.permissions = permissions
        self.log_callback = log_callback      # (guild_id, speaker_label, transcript, reply)
        self.member_lookup = member_lookup    # (guild_id, user_id) -> Member | None
        self.memory = ConversationMemory()
        # Registered officers are transcribed first, then everyone else; oldest first within each group.
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=self.QUEUE_MAX)
        self._seq = itertools.count()
        self._pending: dict[tuple, int] = defaultdict(int)
        self._officers: dict[int, tuple[float, set]] = {}
        self._task: Optional[asyncio.Task] = None
        self._recent: dict[tuple, float] = {}
        self._awake: dict[tuple, float] = {}
        self._go_ahead: dict[tuple, object] = {}
        self.dropped = {"queue_full": 0, "per_user": 0, "stale": 0, "echo": 0}
        self.stats = {"transcribed": 0, "no_wake_word": 0, "answered": 0}
        self.last_heard: Optional[str] = None      # last transcript, so you can see what Whisper thought you said

    def diagnostics_text(self) -> str:
        d, st = self.dropped, self.stats
        busy = d["queue_full"] + d["per_user"] + d["stale"]
        text = (f"waiting {self._queue.qsize()} · heard {st['transcribed']} · answered {st['answered']} · "
                f"no wake word {st['no_wake_word']} · dropped (too busy) {busy} · echo ignored {d['echo']}")
        if self.last_heard:
            text += f"\nLast heard: “{self.last_heard[:100]}”"
        return text

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="voice-commands")
            if self.stt.available():
                asyncio.create_task(self.stt.warmup())   # load Whisper now, not on the first command

    def _is_officer(self, guild_id: int, user_id: int) -> bool:
        cached = self._officers.get(guild_id)
        if cached is None or time.monotonic() - cached[0] > self.OFFICER_CACHE_S:
            asyncio.ensure_future(self._refresh_officers(guild_id))
        return bool(cached and user_id in cached[1])

    async def _refresh_officers(self, guild_id: int) -> None:
        self._officers[guild_id] = (time.monotonic(), self._officers.get(guild_id, (0, set()))[1])
        try:
            rows = await self.core.db.fetchall(
                "SELECT discord_id FROM officers WHERE guild_id = ? AND active = 1", (guild_id,))
            self._officers[guild_id] = (time.monotonic(), {int(r["discord_id"]) for r in rows})
        except Exception:
            log.debug("Officer cache refresh failed", exc_info=True)

    def _echo_context(self, guild_id: int) -> Optional[str]:
        gs = getattr(self.voice, "gs", None)
        if gs is None:
            return None
        g = gs(guild_id)
        if g.speaking or time.monotonic() - g.last_spoken_end < self.ECHO_WINDOW_S:
            return g.last_spoken_text or None
        return None

    def submit(self, guild_id: int, user_id: int, pcm: bytes) -> None:
        key = (guild_id, user_id)
        self._go_ahead.pop(key, None)   # they kept talking - no "go ahead" needed
        if self._pending[key] >= self.PER_USER_MAX:
            self.dropped["per_user"] += 1
            log.debug("Speaker %s already has %d clips waiting; dropping one", user_id, self._pending[key])
            return
        tier = 0 if self._is_officer(guild_id, user_id) else 1
        item = (tier, next(self._seq), guild_id, user_id, pcm, time.monotonic(), self._echo_context(guild_id))
        try:
            self._queue.put_nowait(item)
            self._pending[key] += 1
        except asyncio.QueueFull:
            self.dropped["queue_full"] += 1
            log.warning("Voice command queue full; dropping an utterance from %s", user_id)

    async def _worker(self) -> None:
        while True:
            _tier, _seq, guild_id, user_id, pcm, received, echo_text = await self._queue.get()
            key = (guild_id, user_id)
            self._pending[key] = max(0, self._pending[key] - 1)
            age = time.monotonic() - received
            if age > self.STALE_AFTER_S:
                self.dropped["stale"] += 1
                log.info("Dropping %.1fs-old voice clip from %s (speech-to-text backlog)", age, user_id)
                continue
            try:
                await self._handle(guild_id, user_id, pcm, echo_text)
            except Exception:
                log.exception("Voice command failed")

    async def _transcribe(self, audio, statuses, require_wake: bool):
        """Cheap quick pass first; the accurate model only when needed. Returns (text, woke, rest, intent)."""
        seconds = len(audio) / 16000
        if self.stt.has_quick_model:
            # Short clips normally get one full quick pass. Only when people are queued up behind this clip
            # (busy channel) or the clip is long is just the start screened for the wake word.
            screened = require_wake and seconds > self.SCREEN_S and (seconds > 6 or self._queue.qsize() >= 2)
            quick = await self.stt.quick_transcribe(audio, max_seconds=self.SCREEN_S if screened else None)
            woke, rest = strip_wake_word(quick)
            if woke and not rest and seconds <= self.SCREEN_S + 0.5:
                return quick, True, "", None      # just "Dispatch" - nothing more to transcribe
            if require_wake and not woke and not maybe_wake_word(quick):
                log.debug("Ignored (no wake word): %r", quick[:80])
                return quick, False, rest, None   # clearly not for dispatch: discarded after the cheap pass
            if seconds <= 6 and woke:
                if screened:   # only the start was transcribed - get the whole (short) command
                    quick = await self.stt.quick_transcribe(audio)
                    woke, rest = strip_wake_word(quick)
                intent = parse_rules(rest, statuses) if woke else None
                if intent is not None and intent.clarification is None and intent.name != "unclear":
                    return quick, woke, rest, intent  # clear command - no need for the slower model
        text = await self.stt.transcribe(audio)
        woke, rest = strip_wake_word(text)
        return text, woke, rest, parse_rules(rest, statuses) if woke or not require_wake else None

    async def _handle(self, guild_id: int, user_id: int, pcm: bytes, echo_text: Optional[str] = None) -> None:
        settings = await self.voice.settings(guild_id)
        if not settings["listening"]:
            return
        audio = pcm48k_stereo_to_16k_mono(pcm)
        if rms(audio) < 0.004:
            return  # near-silence
        started = time.monotonic()
        statuses = await self.core.units.status_codes(guild_id)
        akey = (guild_id, user_id)
        awake = time.monotonic() - self._awake.get(akey, 0) < self.AWAKE_S
        need_wake = settings["require_wake_word"] and not awake
        text, woke, rest, rule_intent = await self._transcribe(audio, statuses, need_wake)
        stt_seconds = time.monotonic() - started
        if not text or len(text) < 3:
            return
        if awake and not woke:
            woke = True     # continuing after "Dispatch ... [pause]"
        if woke and rest:
            self._awake.pop(akey, None)
        self.stats["transcribed"] += 1
        self.last_heard = text
        if echo_text and settings.get("half_duplex", True) and looks_like_echo(text, echo_text):
            self.dropped["echo"] += 1
            log.debug("Ignored dispatcher echo from %s: %r", user_id, text[:80])
            return
        if need_wake and not woke:
            self.stats["no_wake_word"] += 1
            return
        member = self.member_lookup(guild_id, user_id)
        if member is None or getattr(member, "bot", False):
            return
        from security.permissions import Actor
        if not await self.permissions.has(guild_id, Actor.from_member(member), "voice.command"):
            await self._reply(guild_id, member.display_name, text,
                              "Dispatch can't take radio traffic from you yet. Your Discord role is not set up as an officer role. "
                              "Ask an admin to run slash permissions map.")
            return
        officer = await self.core.units.officer_by_discord(guild_id, user_id)
        key = (guild_id, user_id, rest.lower())
        now = time.monotonic()
        if now - self._recent.get(key, 0) < 4:
            return  # same transmission picked up twice
        self._recent[key] = now
        speaker = officer["callsign"] if officer and officer.get("callsign") else member.display_name
        if woke and not rest.strip():
            # "Dispatch" on its own: wait for the rest; answer "go ahead" only if they don't continue.
            self._awake[akey] = time.monotonic()
            token = object()
            self._go_ahead[akey] = token
            asyncio.create_task(self._maybe_go_ahead(guild_id, user_id, speaker, text, token))
            return
        intent = rule_intent
        pending = self.memory.pop_pending(guild_id, user_id)
        if pending is not None and (intent is None or intent.name in {"unclear", "report_incident"}):
            from ai.conversation import complete_pending
            intent = complete_pending(pending, rest)
            intent.args["asked_twice"] = pending.get("asked_twice", False)
        if intent is None:
            from ai.conversation import custom_report_intent, understand
            intent = understand(rest, statuses) or custom_report_intent(
                rest, await self.core.db.get_config(guild_id, "custom_categories", []) or [])
        if intent is None and self.core.ai is not None:
            ctx = self.memory.get(guild_id, user_id)
            intent = await parse_with_ai(self.core.ai, guild_id, rest, statuses, ctx,
                                         officer.get("current_incident_id") if officer else None)
            if intent.name == "question":
                intent.args.setdefault("text", rest)
        if intent is None:
            intent = Intent("unclear", clarification="Dispatch, say again?")
        if officer is None and intent.name not in {"query_highest", "query_active", "query_available", "query_bolos", "question",
                                                   "report_incident"}:
            reply = "Unidentified station, state your callsign. Register your unit with slash unit register."
        else:
            reply = await execute_intent(self.core, guild_id, officer, intent, user_id, self.memory, caller=speaker)
        log.info("Voice command from %s: heard %r -> intent %s %s (%s) -> reply %r [speech-to-text %.1fs, total %.1fs]",
                 speaker, text, intent.name, intent.args, intent.source, reply, stt_seconds, time.monotonic() - started)
        self.memory.add(guild_id, user_id, speaker, rest)
        self.memory.add(guild_id, user_id, "Dispatch", reply)
        self.stats["answered"] += 1
        await self._reply(guild_id, speaker, text, reply)

    async def _maybe_go_ahead(self, guild_id: int, user_id: int, speaker: str, transcript: str, token: object) -> None:
        await asyncio.sleep(self.GO_AHEAD_DELAY_S)
        key = (guild_id, user_id)
        if self._go_ahead.get(key) is not token or self._pending[key]:
            return
        gs = getattr(self.voice, "gs", None)
        sink = gs(guild_id).sink if gs else None
        if sink is not None and user_id in getattr(sink, "_buffers", {}):
            return   # still talking - their next transmission will be handled
        self._go_ahead.pop(key, None)
        await self._reply(guild_id, speaker, transcript, f"{speaker}, go ahead.")

    async def _reply(self, guild_id: int, speaker: str, transcript: str, reply: str) -> None:
        await self.core.announcements.enqueue(guild_id, "voice_reply", reply,
                                              dedupe_key=f"reply:{time.time():.3f}", priority=75)
        await self.log_callback(guild_id, speaker, transcript, reply)
