"""Professional dispatch logging.

Every log event is ALWAYS stored in the database (`dispatch_log`) so `/logs view` works even
with no log channel and with every dashboard disabled. If a log channel is configured the
event is also posted there as a clean embed. Administrators choose:

  * the main log channel (existing channel or an auto-created #pulse-logs)
  * which categories are enabled
  * an optional separate channel per category
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord

from core import events as ev
from core.logging_setup import redact
from database.models import PRIORITY_LABEL, dumps, loads

from . import embeds as E

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

LOG_CATEGORIES: dict[str, tuple[str, str]] = {
    # key: (label, icon)
    "calls": ("New 911 calls", "🚨"),
    "incidents": ("New incidents & live incident cards", "📁"),
    "ai_responses": ("AI dispatch responses", "🧠"),
    "assignments": ("Unit assignments", "🚓"),
    "unit_status": ("Unit status changes", "🔁"),
    "backup": ("Backup requests", "🆘"),
    "priority": ("Priority changes", "🎚️"),
    "bolos": ("BOLO announcements", "📢"),
    "closures": ("Incident closures", "🔒"),
    "failed": ("Failed dispatch requests", "⚠️"),
    "admin": ("Administrative changes", "🛠️"),
    "ai_failures": ("AI provider failures", "🔌"),
    "radio": ("Radio traffic (announcements & voice)", "📻"),
    "erlc": ("ER:LC connection changes", "🎮"),
}
DEFAULT_ENABLED = {k: True for k in LOG_CATEGORIES}
CATEGORY_COLOUR = {"calls": E.DANGER, "incidents": E.BRAND, "ai_responses": E.INFO, "assignments": E.OK,
                   "unit_status": E.MUTED, "backup": E.DANGER, "priority": E.WARN, "bolos": E.WARN, "closures": E.MUTED,
                   "failed": E.WARN, "admin": E.INFO, "ai_failures": E.WARN, "radio": E.MUTED, "erlc": E.INFO}

# Audit actions already covered by a more specific category (not repeated as "admin").
_NON_ADMIN_PREFIXES = ("incident.", "unit.", "bolo.", "report.", "test.", "notice.", "officer.", "broadcast")


class Logbook:
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot
        self.db = bot.db
        b = bot.bus
        b.subscribe(ev.INCIDENT_CREATED, self._on_created)
        b.subscribe(ev.INCIDENT_ASSESSED, self._on_assessed)
        b.subscribe(ev.ASSIGNMENT_CHANGED, self._on_assignment)
        b.subscribe(ev.UNIT_UPDATED, self._on_unit)
        b.subscribe(ev.BACKUP_REQUESTED, self._on_backup)
        b.subscribe(ev.INCIDENT_UPDATED, self._on_updated)
        b.subscribe(ev.BOLO_CHANGED, self._on_bolo)
        b.subscribe(ev.INCIDENT_CLOSED, self._on_closed)
        b.subscribe("dispatch.failed", self._on_failed)
        b.subscribe("ai.failure", self._on_ai_failure)
        b.subscribe(ev.ERLC_STATUS, self._on_erlc)
        self._erlc_state: dict[int, str] = {}
        bot.audit.listeners.append(self._on_audit)
        self._last_ai_source: dict[int, str] = {}

    # ------------------------------------------------------------------ settings
    async def settings(self, guild_id: int) -> dict:
        s = await self.db.get_config(guild_id, "log_settings", {}) or {}
        cats = {**DEFAULT_ENABLED, **(s.get("categories") or {})}
        return {"categories": cats, "channels": s.get("channels") or {}}

    async def set_category(self, guild_id: int, category: str, enabled: bool) -> None:
        s = await self.db.get_config(guild_id, "log_settings", {}) or {}
        s.setdefault("categories", {})[category] = bool(enabled)
        await self.db.set_config(guild_id, "log_settings", s)

    async def set_all(self, guild_id: int, enabled: bool) -> None:
        s = await self.db.get_config(guild_id, "log_settings", {}) or {}
        s["categories"] = {k: bool(enabled) for k in LOG_CATEGORIES}
        await self.db.set_config(guild_id, "log_settings", s)

    async def set_category_channel(self, guild_id: int, category: str, channel_id: Optional[int]) -> None:
        s = await self.db.get_config(guild_id, "log_settings", {}) or {}
        chans = s.setdefault("channels", {})
        if channel_id:
            chans[category] = int(channel_id)
        else:
            chans.pop(category, None)
        await self.db.set_config(guild_id, "log_settings", s)

    async def enabled(self, guild_id: int, category: str) -> bool:
        return bool((await self.settings(guild_id))["categories"].get(category, True))

    async def channel_for(self, guild_id: int, category: str) -> Optional[discord.TextChannel]:
        s = await self.settings(guild_id)
        cid = s["channels"].get(category)
        if cid:
            ch = self.bot.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                return ch
        return await self.bot.log_channel(guild_id)

    # ------------------------------------------------------------------ core
    async def write(self, guild_id: int, category: str, title: str, *, description: Optional[str] = None,
                    fields: Optional[list[tuple[str, str, bool]]] = None, colour: Optional[int] = None,
                    incident_id: Optional[int] = None, actor_id: Optional[int] = None, post: bool = True,
                    embed: Optional[discord.Embed] = None) -> None:
        """Store a log record and (if enabled) post it to the log channel."""
        if not await self.enabled(guild_id, category):
            return
        detail = {"description": redact(description or "")[:1500], "fields": [(n, redact(v)[:500], i) for n, v, i in (fields or [])]}
        try:
            await self.db.execute(
                "INSERT INTO dispatch_log(guild_id, category, title, detail, incident_id, actor_id) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, category, title[:200], dumps(detail), incident_id, actor_id))
        except Exception:
            log.exception("Could not store dispatch log entry")
        if not post:
            return
        ch = await self.channel_for(guild_id, category)
        if ch is None:
            return
        if embed is None:
            look = await E.look_for(self.bot, guild_id)
            icon = LOG_CATEGORIES.get(category, ("", "📋"))[1]
            embed = E.base(f"{icon} {title}", description=detail["description"] or None,
                           colour=colour if colour is not None else CATEGORY_COLOUR.get(category), look=look)
            for name, value, inline in detail["fields"][:12]:
                embed.add_field(name=name[:256], value=(value or "—")[:1024], inline=inline)
            if actor_id:
                embed.add_field(name="By", value=f"<@{actor_id}>", inline=True)
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Could not post log entry to #%s: %s", getattr(ch, "name", "?"), e)

    async def recent(self, guild_id: int, category: Optional[str] = None, limit: int = 15,
                     incident_id: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM dispatch_log WHERE guild_id = ?"
        params: list = [guild_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if incident_id:
            sql += " AND incident_id = ?"
            params.append(incident_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in await self.db.fetchall(sql, params):
            d = dict(r)
            d["detail"] = loads(d.get("detail"), {}) or {}
            out.append(d)
        return out

    async def counts(self, guild_id: int, since_iso: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT category, COUNT(*) AS n FROM dispatch_log WHERE guild_id = ? AND at >= ? GROUP BY category", (guild_id, since_iso))
        return {r["category"]: r["n"] for r in rows}

    # ------------------------------------------------------------------ helpers
    async def _inc(self, incident_id: Optional[int]) -> Optional[dict]:
        return await self.bot.core.incidents.get(incident_id) if incident_id else None

    def _inc_fields(self, inc: dict) -> list[tuple[str, str, bool]]:
        return [("Incident", f"#{inc['number']} · {E.safe(inc.get('nature') or 'Unknown', 80)}", True),
                ("Location", E.safe(inc.get("location") or "Not provided", 150), True),
                ("Priority", E.priority_text(inc), True)]

    # ------------------------------------------------------------------ event handlers
    async def _on_created(self, e: ev.Event) -> None:
        await asyncio.sleep(0.4)  # let the instant assessment land first (priority/recommendations)
        inc = await self._inc(e.data.get("incident_id"))
        if inc is None:
            return
        category = "calls" if inc["source"] in {"erlc", "discord"} else "incidents"
        title = {"erlc": "In-Game 911 Call", "discord": "Discord 911 Call"}.get(inc["source"], "New Incident")
        fields = self._inc_fields(inc) + [
            ("Source", E.source_text(inc), True),
            ("Verification", {"verified": "✅ API-verified", "unverified": "⚠️ Unverified",
                              "test": "🧪 Test data"}.get(inc["verification"], inc["verification"]), True),
            ("Caller", E.safe(inc.get("caller_display") or "Unknown", 60), True),
        ]
        if inc.get("description") and inc.get("description") != inc.get("nature"):
            fields.append(("Details", E.safe(inc["description"], 500), False))
        await self.write(inc["guild_id"], category, title, fields=fields, incident_id=inc["id"], actor_id=inc.get("created_by"),
                         colour=E.PRIORITY_COLOUR.get(E.priority_of(inc), E.DANGER))

    async def _on_assessed(self, e: ev.Event) -> None:
        inc = await self._inc(e.data.get("incident_id"))
        if inc is None or not str(inc.get("assessment_source") or "").startswith("ai"):
            return
        marker = f"{inc['id']}:{inc.get('assessment_source')}:{inc.get('ai_priority')}:{inc.get('recommended_units')}"
        if self._last_ai_source.get(inc["id"]) == marker:
            return
        self._last_ai_source[inc["id"]] = marker
        await self.write(e.guild_id, "ai_responses", f"AI Assessment · Incident #{inc['number']}",
                         description=E.safe(inc.get("recommendation_reason") or "", 800),
                         fields=[("Recommended priority", PRIORITY_LABEL.get(inc.get("ai_priority") or "", "—"), True),
                                 ("Recommended units", E.recommended_text(inc), True),
                                 ("Provider", str(inc.get("assessment_source"))[3:] or "—", True)],
                         incident_id=inc["id"])

    async def _on_assignment(self, e: ev.Event) -> None:
        inc = await self._inc(e.data.get("incident_id"))
        unit = await self.bot.core.units.unit(e.data["unit_id"]) if e.data.get("unit_id") else None
        if inc is None or unit is None:
            return
        attach = e.data.get("action") == "attach"
        await self.write(e.guild_id, "assignments",
                         f"Unit {'Assigned' if attach else 'Cleared'} · {unit['callsign']}",
                         fields=[("Unit", f"`{unit['callsign']}` ({unit.get('dept_code') or '—'})", True)] + self._inc_fields(inc)
                         + ([("Reason", E.safe(e.data.get("reason"), 100), True)] if e.data.get("reason") and not attach else []),
                         colour=E.OK if attach else E.MUTED, incident_id=inc["id"], actor_id=e.data.get("actor_id"))

    async def _on_unit(self, e: ev.Event) -> None:
        if not e.data.get("unit_id") or not e.data.get("new_code"):
            return
        unit = await self.bot.core.units.unit(e.data["unit_id"])
        if unit is None:
            return
        await self.write(e.guild_id, "unit_status", f"Status Change · {unit['callsign']}",
                         fields=[("Unit", f"`{unit['callsign']}`", True),
                                 ("From", str(e.data.get("old_code") or "—"), True),
                                 ("To", f"**{e.data['new_code']}** {unit.get('status_label') or ''}", True)],
                         actor_id=e.data.get("actor_id"))

    async def _on_backup(self, e: ev.Event) -> None:
        unit = await self.bot.core.units.unit(e.data["unit_id"]) if e.data.get("unit_id") else None
        inc = await self._inc(e.data.get("incident_id"))
        urgency = e.data.get("urgency", "routine")
        fields = [("Requested by", f"`{unit['callsign']}`" if unit else "Unknown unit", True),
                  ("Urgency", {"emergency": "🔴 Emergency", "urgent": "🟠 Urgent"}.get(urgency, "🔵 Routine"), True)]
        if inc:
            fields += self._inc_fields(inc)[:2]
        await self.write(e.guild_id, "backup", "Backup Requested", fields=fields,
                         colour=E.DANGER if urgency != "routine" else E.WARN, incident_id=inc["id"] if inc else None)

    async def _on_updated(self, e: ev.Event) -> None:
        if "priority" not in (e.data.get("changes") or []):
            return
        inc = await self._inc(e.data.get("incident_id"))
        if inc is None:
            return
        old, new = e.data.get("old_priority"), e.data.get("new_priority")
        await self.write(e.guild_id, "priority", f"Priority Changed · Incident #{inc['number']}",
                         fields=[("From", PRIORITY_LABEL.get(old or "", "Unassessed"), True),
                                 ("To", f"**{PRIORITY_LABEL.get(new or '', new or '—')}**", True),
                                 ("Status", str(inc.get("priority_status") or "—").title(), True)]
                         + ([("Reason", E.safe(inc.get("priority_reason"), 300), False)] if inc.get("priority_reason") else []),
                         colour=E.PRIORITY_COLOUR.get(new or "", E.WARN), incident_id=inc["id"], actor_id=inc.get("priority_approved_by"))

    async def _on_bolo(self, e: ev.Event) -> None:
        b = await self.bot.core.bolos.get(e.data["bolo_id"])
        if b is None:
            return
        action = e.data.get("action", "updated")
        await self.write(e.guild_id, "bolos", f"BOLO #{b['number']} {action.title()}",
                         fields=[("Type", str(b.get("kind") or "other").title(), True),
                                 ("Subject", E.safe(b.get("subject"), 200), True)]
                         + ([("Details", E.safe(b.get("description"), 500), False)] if b.get("description") else []),
                         colour=E.WARN if action != "cleared" else E.MUTED)

    async def _on_closed(self, e: ev.Event) -> None:
        inc = await self._inc(e.data.get("incident_id"))
        if inc is None:
            return
        from database.models import parse_iso
        a, b = parse_iso(inc.get("created_at")), parse_iso(inc.get("closed_at"))
        dur = f"{int((b - a).total_seconds() // 60)} min" if a and b else "—"
        await self.write(e.guild_id, "closures", f"Incident #{inc['number']} Closed",
                         fields=self._inc_fields(inc)[:2] + [("Duration", dur, True)]
                         + ([("Reason", E.safe(inc.get("close_reason"), 300), False)] if inc.get("close_reason") else []),
                         colour=E.MUTED, incident_id=inc["id"], actor_id=inc.get("closed_by"))

    async def _on_failed(self, e: ev.Event) -> None:
        await self.write(e.guild_id, "failed", "Dispatch Request Not Understood",
                         description=f"> {E.safe(e.data.get('text'), 600)}",
                         fields=[("Reply", E.safe(e.data.get("reply"), 300), False),
                                 ("Channel", str(e.data.get("channel") or "—"), True)],
                         actor_id=e.data.get("actor_id"))

    async def _on_ai_failure(self, e: ev.Event) -> None:
        from ai.providers.catalog import SPEC
        sp = SPEC.get(e.data.get("provider", ""))
        kind = e.data.get("kind")
        hint = {"quota": "Rate limit or free quota reached - the next provider was used automatically.",
                "auth": "The API key was rejected. Check it with /ai-provider key.",
                "model": "The model is unavailable - another model is selected automatically.",
                "timeout": "The provider was too slow and was skipped.",
                "unavailable": "The provider is unreachable or having an outage."}.get(kind, "Request failed.")
        await self.write(e.guild_id, "ai_failures", f"AI Provider Issue · {sp.label if sp else e.data.get('provider')}",
                         description=hint,
                         fields=[("Type", str(kind), True), ("Used for", str(e.data.get("purpose")), True),
                                 ("Detail", E.safe(e.data.get("message"), 300), False)])

    async def _on_erlc(self, e: ev.Event) -> None:
        """Log only *changes* of the ER:LC connection (not every poll)."""
        status = e.data.get("status")
        if not status or e.data.get("poll") and self._erlc_state.get(e.guild_id) == status:
            return
        previous = self._erlc_state.get(e.guild_id)
        self._erlc_state[e.guild_id] = status
        if previous == status or (previous is None and status in {"connected", "not_configured", "disabled"}):
            return  # first status after a restart is not news unless it is a problem
        title = {"connected": "ER:LC API Connected", "error": "ER:LC API Error", "rate_limited": "ER:LC API Rate Limited",
                 "not_configured": "ER:LC API Not Configured", "disabled": "ER:LC Monitoring Disabled"}.get(status, f"ER:LC: {status}")
        colour = E.OK if status == "connected" else (E.DANGER if status == "error" else E.WARN)
        desc = E.safe(redact(e.data.get("detail") or ""), 500) or None
        if status == "connected" and previous in {"error", "rate_limited"}:
            desc = "Connection restored. In-game 911 calls are being received again."
        await self.write(e.guild_id, "erlc", title, description=desc, colour=colour)

    async def _on_audit(self, guild_id: int, actor_id: Optional[int], action: str, target: Optional[str], detail) -> None:
        if action.startswith(_NON_ADMIN_PREFIXES):
            return
        fields = [("Action", f"`{action}`", True)]
        if target:
            fields.append(("Target", E.safe(target, 100), True))
        if detail:
            fields.append(("Details", E.safe(redact(dumps(detail)), 500), False))
        await self.write(guild_id, "admin", "Configuration Changed", fields=fields, actor_id=actor_id)

    async def radio(self, guild_id: int, text: str, *, status: str = "played", error: Optional[str] = None,
                    speaker: Optional[str] = None, transcript: Optional[str] = None) -> None:
        if speaker is not None:
            fields = [(f"🎙️ {speaker[:80]}", E.safe(transcript, 900), False), ("📡 Dispatch", E.safe(text, 900), False)]
            await self.write(guild_id, "radio", "Radio Traffic", fields=fields, colour=E.MUTED)
            return
        suffix = {"played": "", "muted": "\n*Speech muted — text only.*",
                  "failed": f"\n*Voice playback failed: {E.safe(error, 200)}*"}.get(status, "")
        await self.write(guild_id, "radio", "Dispatch Broadcast", description=f"“{E.safe(text, 1800)}”{suffix}", colour=E.MUTED)
