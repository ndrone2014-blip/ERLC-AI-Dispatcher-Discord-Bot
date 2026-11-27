"""Pulse Assistant: the AI dispatcher assistant behind /ask and the dashboard's "Ask Pulse" box.

Simple requests ("show active calls", "any units available?", "start my shift") are recognised
locally in well under a millisecond and answered straight from the database: no AI request.
Only conversational questions that no intent matches go to the configured AI provider, and
then with the real current situation as context and strict instructions never to invent
incidents, units, members or numbers.

Every answer respects the asker's permissions, and actions (close an incident, start/end a
shift) are never executed directly: they come back as a confirmation the user must accept.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from database.models import PRIORITY_LABEL

from .common import ServiceError, fmt_duration

if TYPE_CHECKING:
    from .platform import CommunityPlatform

# word -> concept. Several spellings / synonyms map onto one concept so phrasing and word order don't matter.
CONCEPTS: dict[str, set[str]] = {
    "INCIDENT": {"call", "calls", "incident", "incidents", "emergency", "emergencies", "911", "911s", "cad", "scene", "scenes",
                 "dispatches", "jobs", "insident", "incedent", "incidnet", "callz", "case", "cases"},
    "DISPATCHLIST": {"dispatch", "board", "queue", "list"},
    "ACTIVE": {"active", "open", "current", "currently", "now", "ongoing", "pending", "live", "right", "today", "existing", "outstanding"},
    "SHOW": {"show", "display", "list", "see", "view", "give", "what", "whats", "which", "any", "are", "there", "tell", "get", "check", "how", "many"},
    "UNIT": {"unit", "units", "officer", "officers", "cars", "car", "responders", "callsign", "callsigns", "patrol", "patrols", "crews"},
    "AVAILABLE": {"available", "free", "10-8", "108", "idle", "avail", "availible", "avalable", "ready"},
    "DEPARTMENT": {"department", "departments", "dept", "depts", "agency", "agencies"},
    "SERVER": {"server", "erlc", "ingame", "in-game", "game", "players", "player", "playercount", "online"},
    "STAFF": {"staff", "team", "mods", "moderators", "admins"},
    "SHIFT": {"shift", "shifts", "duty", "clock", "clocked", "on-duty", "onduty"},
    "START": {"start", "begin", "clock-in", "in", "go", "going", "starting", "open"},
    "END": {"end", "stop", "finish", "out", "clock-out", "off", "ending", "done"},
    "SELF": {"my", "me", "i", "im", "i'm", "mine", "myself"},
    "VERIFY": {"verify", "verified", "verification", "roblox", "linked", "link"},
    "SESSION": {"session", "sessions", "event", "events", "training", "trainings"},
    "APPLICATION": {"application", "applications", "apps", "app", "applicants"},
    "MODERATION": {"warnings", "warning", "warns", "history", "punishments", "infractions", "record", "bans", "cases"},
    "HELP": {"help", "commands", "command", "how", "guide", "usage", "use"},
    "STATUS": {"status", "ping", "latency", "uptime", "health", "working"},
    "STATS": {"stats", "statistics", "numbers", "summary", "report", "overview"},
    "CLOSE": {"close", "resolve", "clear", "finish", "end", "complete"},
    "BOLO": {"bolo", "bolos", "lookout", "wanted"},
}
_WORD = re.compile(r"[a-z0-9'\-]+")
_NUM = re.compile(r"(?:#|incident\s*|call\s*|inc[-\s]?)(\d{1,6})\b|\b(\d{2,6})\b")


def concepts(text: str) -> tuple[set[str], list[str]]:
    words = _WORD.findall(text.lower())
    found = set()
    for w in words:
        for c, vocab in CONCEPTS.items():
            if w in vocab:
                found.add(c)
    return found, words


@dataclass
class Intent:
    name: str
    confidence: float
    args: dict = field(default_factory=dict)


def recognise(text: str) -> Optional[Intent]:
    t = (text or "").strip()
    if not t:
        return None
    c, words = concepts(t)
    low = " ".join(words)
    num = None
    m = _NUM.search(low)
    if m:
        num = int(m.group(1) or m.group(2))

    def has(*names: str) -> bool:
        return all(n in c for n in names)

    # actions (always confirmed by the user before anything happens)
    question = any(x in low for x in ("who", "how long", "how many", "anyone", "anybody", "list", "show")) \
        or low.startswith(("am ", "is ", "are ", "do ", "did ", "was "))
    if "SHIFT" in c and not question:
        if "END" in c or "clock out" in low or "off duty" in low:
            return Intent("shift_end", 0.9)
        if "START" in c or "clock in" in low or "on duty" in low:
            return Intent("shift_start", 0.9)
    if "CLOSE" in c and num is not None and ("INCIDENT" in c or "#" in t):
        return Intent("close_incident", 0.85, {"number": num})
    # questions
    if "INCIDENT" in c and num is not None and "CLOSE" not in c:
        return Intent("incident_detail", 0.9, {"number": num})
    if has("SHIFT") and ("SELF" in c and any(x in low for x in ("how long", "my shift", "am i", "hours", "time"))):
        return Intent("my_shift", 0.85)
    if "SHIFT" in c or (has("STAFF") and ("ACTIVE" in c or "SHOW" in c or "online" in low or "on" in words)):
        return Intent("staff_on_shift", 0.8)
    if "BOLO" in c:
        return Intent("bolos", 0.85)
    if "UNIT" in c and "AVAILABLE" in c:
        return Intent("units_available", 0.9)
    if "UNIT" in c:
        return Intent("units", 0.85)
    if "INCIDENT" in c or ("DISPATCHLIST" in c and ("SHOW" in c or "ACTIVE" in c)):
        return Intent("active_incidents", 0.9 if ("ACTIVE" in c or "SHOW" in c) else 0.7)
    if "DEPARTMENT" in c:
        return Intent("departments", 0.85)
    if "VERIFY" in c and ("SELF" in c or "am" in words):
        return Intent("my_verification", 0.85)
    if "VERIFY" in c:
        return Intent("verification_help", 0.7)
    if "SESSION" in c:
        return Intent("sessions", 0.85)
    if "APPLICATION" in c:
        return Intent("applications", 0.8)
    if "MODERATION" in c and "SELF" in c:
        return Intent("my_history", 0.8)
    if "SERVER" in c:
        return Intent("server_info", 0.8)
    if "STATUS" in c and ("bot" in words or "you" in words or "pulse" in words or len(words) <= 3):
        return Intent("bot_status", 0.8)
    if "STATS" in c:
        return Intent("stats", 0.75)
    if "HELP" in c and not ("INCIDENT" in c or "UNIT" in c):
        return Intent("help", 0.7)
    return None


@dataclass
class Reply:
    title: str
    text: str = ""
    lines: list[str] = field(default_factory=list)
    intent: Optional[str] = None
    source: str = "local"             # local | ai | none
    confirm: Optional[dict] = None    # {"action": ..., "args": {...}, "prompt": "..."}
    ok: bool = True
    latency_ms: float = 0.0

    def as_dict(self) -> dict:
        return {"title": self.title, "text": self.text, "lines": self.lines, "intent": self.intent, "source": self.source,
                "confirm": self.confirm, "ok": self.ok, "latency_ms": round(self.latency_ms, 2)}


SYSTEM_PROMPT = (
    "You are Pulse, the calm, professional dispatcher assistant of an Emergency Response: Liberty County (ER:LC) roleplay "
    "community on Discord. Answer in at most 4 short sentences. Use ONLY the facts in the SITUATION block. Never invent "
    "incidents, units, members, numbers or server information; if the answer is not in the facts, say plainly that you "
    "don't have that information and suggest the relevant command. Never claim you performed an action. This is roleplay, "
    "not a real emergency service.")


class Assistant:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p

    async def ask(self, guild_id: int, user_id: int, permissions: set[str], text: str, *, allow_ai: bool = True) -> Reply:
        t0 = time.perf_counter()
        intent = recognise(text)
        if intent is not None:
            try:
                reply = await self._answer(guild_id, user_id, permissions, intent)
            except ServiceError as e:
                reply = Reply("Pulse", str(e), ok=False)
            reply.intent = intent.name
        elif allow_ai and self.p.ai is not None:
            reply = await self._ai(guild_id, user_id, permissions, text)
        else:
            reply = Reply("Pulse", "I couldn't match that to something I can look up. Try asking about active calls, available units, "
                                   "your shift, sessions or verification — or run `/help`.", source="none", ok=False)
        reply.latency_ms = (time.perf_counter() - t0) * 1000
        return reply

    def _deny(self, what: str) -> Reply:
        return Reply("Not available", f"You don't have permission to view {what}.", ok=False)

    async def _answer(self, g: int, uid: int, perms: set[str], it: Intent) -> Reply:
        d = self.p.dispatch
        n = it.name
        if n in {"active_incidents", "incident_detail", "close_incident", "units", "units_available", "bolos"} and d is None:
            return Reply("Dispatch", "The dispatch system is not running.", ok=False)
        if n == "active_incidents":
            if "incident.view" not in perms:
                return self._deny("incidents")
            active = await d.incidents.list_active(g)
            if not active:
                return Reply("Active Incidents", "There are no active incidents right now.")
            lines = []
            for inc in active[:15]:
                units = await d.incidents.assigned_units(inc["id"])
                p = inc.get("priority") or inc.get("ai_priority")
                lines.append(f"#{inc['number']} · {PRIORITY_LABEL.get(p or '', 'Assessing')} · {inc.get('nature') or 'Unknown'} · "
                             f"{inc.get('location') or 'no location'} · {len(units)} unit(s)")
            more = f" Showing 15 of {len(active)}." if len(active) > 15 else ""
            return Reply("Active Incidents", f"{len(active)} active incident(s).{more}", lines)
        if n == "incident_detail":
            if "incident.view" not in perms:
                return self._deny("incidents")
            inc = await d.incidents.get_by_number(g, it.args["number"])
            if not inc:
                return Reply("Incident", f"There is no incident #{it.args['number']}.", ok=False)
            units = await d.incidents.assigned_units(inc["id"])
            p = inc.get("priority") or inc.get("ai_priority")
            return Reply(f"Incident #{inc['number']}", f"{inc.get('nature') or 'Unknown'} at {inc.get('location') or 'an unknown location'}.", [
                f"Status: {inc['status']}", f"Priority: {PRIORITY_LABEL.get(p or '', 'Assessing')}",
                f"Units: {', '.join(u['callsign'] for u in units) or 'none assigned'}",
                f"Source: {inc['source']} ({inc['verification']})"])
        if n == "close_incident":
            if "incident.close" not in perms:
                return self._deny("incident closing")
            inc = await d.incidents.get_by_number(g, it.args["number"])
            if not inc or inc["status"] == "closed":
                return Reply("Close Incident", f"Incident #{it.args['number']} is not open.", ok=False)
            return Reply("Close Incident", f"Close incident #{inc['number']} ({inc.get('nature') or 'Unknown'})?",
                         confirm={"action": "close_incident", "args": {"number": inc["number"]},
                                  "prompt": f"Close incident #{inc['number']}"})
        if n in {"units", "units_available"}:
            if "incident.view" not in perms:
                return self._deny("units")
            units = await (d.units.available_units(g) if n == "units_available" else d.units.list_units(g))
            if not units:
                return Reply("Units", "No units are available right now." if n == "units_available" else "No units are on duty right now.")
            lines = [f"{u['callsign']} ({u.get('dept_code') or '—'}) · {u['status_code']} {u.get('status_label') or ''}"
                     f"{' · on #' + str(u['incident_number']) if u.get('incident_number') else ''}" for u in units[:20]]
            return Reply("Available Units" if n == "units_available" else "Units on Duty", f"{len(units)} unit(s).", lines)
        if n == "bolos":
            if "bolo.view" not in perms:
                return self._deny("BOLOs")
            bolos = await d.bolos.active(g)
            return Reply("Active BOLOs", f"{len(bolos)} active BOLO(s)." if bolos else "There are no active BOLOs.",
                         [f"#{b['number']} {b['kind']}: {b['subject']}" for b in bolos[:15]])
        if n == "departments":
            if d is None:
                return Reply("Departments", "The dispatch system is not running.", ok=False)
            deps = await d.units.departments(g)
            return Reply("Departments", f"{len(deps)} department(s).", [f"{x['short_code']} — {x['name']}" for x in deps])
        if n == "staff_on_shift":
            if "staff.view" not in perms and "shift.manage" not in perms:
                return self._deny("staff activity")
            active = await self.p.shifts.active(g)
            if not active:
                return Reply("On Shift", "Nobody is on shift right now.")
            return Reply("On Shift", f"{len(active)} member(s) on shift.",
                         [f"{a.get('display_name') or a.get('username') or a['user_id']} · {a['shift_type']} · {fmt_duration(a['elapsed_s'])}"
                          for a in active[:20]])
        if n == "my_shift":
            s = await self.p.shifts.summary(g, uid)
            cur = s["current"]
            text = "You are not on shift." if not cur else f"You are on a **{cur['shift_type']}** shift."
            return Reply("Your Shifts", text, [f"This week: {s['week_text']}", f"All time: {s['total_text']} over {s['count']} shift(s)"])
        if n == "shift_start":
            if "shift.self" not in perms:
                return self._deny("shifts")
            if await self.p.shifts.current(g, uid):
                return Reply("Shift", "You already have a shift running.", ok=False)
            types = (await self.p.shifts.settings(g))["types"]
            return Reply("Start Shift", f"Start a **{types[0]}** shift now?",
                         confirm={"action": "shift_start", "args": {"shift_type": types[0]}, "prompt": "Start shift"})
        if n == "shift_end":
            cur = await self.p.shifts.current(g, uid)
            if not cur:
                return Reply("Shift", "You don't have a shift running.", ok=False)
            return Reply("End Shift", f"End your {cur['shift_type']} shift?", confirm={"action": "shift_end", "args": {}, "prompt": "End shift"})
        if n == "my_verification":
            link = await self.p.verification.get_link(g, uid)
            if link:
                return Reply("Verification", f"You are verified as **{link['roblox_username']}** on Roblox.")
            return Reply("Verification", "You are not verified yet. Run `/verify` to link your Roblox account.")
        if n == "verification_help":
            return Reply("Verification", "Run `/verify` and follow the steps to link your Roblox account. Staff can check a member with "
                                         "`/verification lookup`.")
        if n == "sessions":
            sessions = await self.p.sessions.list(g, "upcoming", 10)
            if not sessions:
                return Reply("Sessions", "No sessions are scheduled.")
            from .sessions import _ts
            return Reply("Sessions", f"{len(sessions)} upcoming or active session(s).",
                         [f"#{s['number']} {s['name']} · {s['status']} · {_ts(s.get('started_at') or s.get('scheduled_for'), 'R')}" for s in sessions])
        if n == "applications":
            if "application.review" in perms:
                c = await self.p.applications.counts(g)
                return Reply("Applications", f"{c['open']} application(s) waiting for review.")
            forms = await self.p.applications.forms(g, enabled_only=True)
            return Reply("Applications", "Open applications: " + (", ".join(f["name"] for f in forms) or "none right now") +
                         ". Apply with `/apply`.")
        if n == "my_history":
            cases = [c for c in await self.p.moderation.history(g, uid) if c["status"] == "active"]
            return Reply("Your Record", f"You have {len(cases)} active moderation case(s)." if cases else "You have no active moderation cases.",
                         [f"#{c['number']} {c['action']} · {c['reason']}" for c in cases[:10]])
        if n == "server_info":
            row = await self.p.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (g,))
            if not row or not row["connected"]:
                return Reply("ER:LC Server", "The ER:LC server is not connected, so I can't see in-game information right now.", ok=False)
            return Reply("ER:LC Server", f"**{row['server_name'] or 'ER:LC server'}** is connected.",
                         [f"Players: {row['current_players']}/{row['max_players']}", f"Last update: {row['last_success_at'] or '—'}"])
        if n == "bot_status":
            return Reply("Pulse Status", "I'm online and answering. Use `/ping` for detailed latency.")
        if n == "stats":
            if "analytics.view" not in perms and "stats.view" not in perms:
                return self._deny("statistics")
            data = await self.p.analytics.series(g, ["incidents", "member_joins", "verifications", "moderation"], "7d")
            return Reply("Last 7 days", "", [f"{m['label']}: {m['total']}" for m in data["metrics"].values()])
        if n == "help":
            return Reply("What I can do", "Ask me things like:", [
                "“Are there any active calls?” · “Show incident 104”", "“Which units are available?”", "“Who is on shift?” · “Start my shift”",
                "“Am I verified?” · “Any sessions today?”", "Run `/help` for every command."])
        return Reply("Pulse", "I couldn't answer that.", ok=False)

    async def _ai(self, g: int, uid: int, perms: set[str], text: str) -> Reply:
        facts = []
        if "incident.view" in perms and self.p.dispatch is not None:
            facts.append(await self.p.dispatch.situation_text(g))
        link = await self.p.verification.get_link(g, uid)
        facts.append(f"ASKER: verified Roblox account: {link['roblox_username'] if link else 'not verified'}")
        sessions = await self.p.sessions.list(g, "upcoming", 5)
        facts.append("SESSIONS: " + ("; ".join(f"#{s['number']} {s['name']} ({s['status']})" for s in sessions) or "none"))
        prompt = "SITUATION:\n" + "\n".join(facts) + f"\n\nQUESTION: {text[:500]}"
        try:
            resp = await self.p.ai.generate(g, purpose="assistant", system=SYSTEM_PROMPT, prompt=prompt, max_tokens=220)
        except Exception:
            return Reply("Pulse", "I couldn't match that to something I can look up, and no AI provider is available right now. "
                                  "Try asking about active calls, units, shifts or sessions — or run `/help`.", source="none", ok=False)
        answer = getattr(resp, "text", None) or str(resp)
        return Reply("Pulse", answer.strip()[:1500], source="ai")

    async def confirm(self, guild_id: int, user_id: int, permissions: set[str], action: str, args: dict[str, Any]) -> Reply:
        """Run an action the user confirmed. Permissions are checked again here."""
        if action == "close_incident":
            if "incident.close" not in permissions or self.p.dispatch is None:
                return self._deny("incident closing")
            inc = await self.p.dispatch.incidents.get_by_number(guild_id, int(args.get("number", 0)))
            if not inc or inc["status"] == "closed":
                return Reply("Close Incident", "That incident is not open.", ok=False)
            await self.p.dispatch.incidents.close(inc["id"], user_id, "Closed via Pulse Assistant")
            return Reply("Close Incident", f"Incident #{inc['number']} closed.")
        if action == "shift_start":
            if "shift.self" not in permissions:
                return self._deny("shifts")
            s = await self.p.shifts.start(guild_id, user_id, str(args.get("shift_type") or ""))
            return Reply("Shift Started", f"Your {s['shift_type']} shift has started. Stay safe out there.")
        if action == "shift_end":
            s = await self.p.shifts.end(guild_id, user_id, user_id)
            return Reply("Shift Ended", f"Shift ended after {fmt_duration(s['duration_s'])}.")
        return Reply("Pulse", "Unknown action.", ok=False)
