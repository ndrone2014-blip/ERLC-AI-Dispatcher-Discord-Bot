"""Dashboard JSON API (framework-independent).

`web/server.py` authenticates the request, resolves the Discord member and their permissions
for the selected server, and calls `Api.dispatch`. Every route declares the permission it needs
and is always scoped to that one server: the guild id comes from the URL and is checked against
the user's real membership on the backend, never trusted from the browser.

Snowflake IDs are sent as strings (JavaScript numbers cannot hold them exactly).
"""
from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from community.common import ServiceError, ValidationError, as_int, clean_text, iso_now, loads, paginate_sql, row_dicts
from database.models import PRIORITIES, PRIORITY_LABEL, dumps, parse_iso, utcnow


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Ctx:
    guild_id: int
    user_id: int
    perms: set[str]
    role_ids: set[int] = field(default_factory=set)
    query: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    username: str = ""

    def need(self, perm: str) -> None:
        if perm not in self.perms:
            raise ApiError(403, "You don't have permission to do that.")

    def q(self, key: str, default: str = "") -> str:
        return str(self.query.get(key, default) or default)

    def qi(self, key: str, default: int = 1) -> int:
        try:
            return int(self.query.get(key, default))
        except (TypeError, ValueError):
            return default

    def b(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)


Handler = Callable[[Ctx], Awaitable[Any]]
ROUTES: list[tuple[str, str, str, Optional[str]]] = []


def route(method: str, path: str, perm: Optional[str]):
    def deco(fn):
        ROUTES.append((method, path, fn.__name__, perm))
        return fn
    return deco


def _compile(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[A-Za-z0-9_.\-]+)", path) + "$")


def _sid(v: Any) -> Optional[str]:
    return str(v) if v not in (None, "", 0) else None


def _opt_int(v: Any) -> Optional[int]:
    return as_int(v, required=False) if v not in (None, "", "0", 0) else None


# Dashboard sections -> the permission(s) that unlock them (a tuple means "any of these"). The frontend hides what
# you can't open, but every API route checks again on its own.
NAV = [
    ("Overview", [("dashboard", "Dashboard", "dashboard.view"), ("activity", "Activity feed", "dashboard.view"),
                  ("analytics", "Analytics", "analytics.view")]),
    ("ER:LC", [("erlc", "Live server", "erlc.view"), ("erlc/players", "Players", "erlc.view")]),
    ("Moderation", [("moderation", "Moderation center", ("mod.view", "erlc.view")), ("moderation/records", "In-game records", "erlc.view"),
                    ("moderation/discord", "Discord cases", "mod.view"), ("moderation/active", "Active punishments", ("mod.view", "erlc.view")),
                    ("moderation/reports", "Member reports", "mod.reports_manage"), ("moderation/staff", "Staff records", ("mod.view", "erlc.view")),
                    ("moderation/settings", "Moderation settings", "admin.config")]),
    ("BOLOs", [("bolos", "BOLO board", "bolo.view")]),
    ("Staff", [("staff/overview", "Staff overview", "staff.view"), ("shifts", "Shifts", "shift.self"), ("staff/leave", "Leave (LOA)", "shift.self"),
               ("staff", "Staff directory", "staff.view"), ("staff/settings", "Staff settings", "staff.manage")]),
    ("Dispatch", [("dispatch", "Dispatch center", "incident.view"), ("incidents", "Incident history", "incident.view"),
                  ("units", "Units", "incident.view"), ("dispatch/settings", "Dispatch settings", "admin.config")]),
    ("Community", [("members", "Members", "members.view"), ("verification", "Verification", "verification.manage"),
                   ("applications", "Applications", "application.review"), ("sessions", "Sessions", "session.view"),
                   ("announcements", "Announcements", "announcement.send")]),
    ("Automation", [("automation", "Automations", "automation.manage"), ("automation/schedules", "Scheduled messages", "automation.manage")]),
    ("Logging", [("logs", "Log viewer", "logs.view"), ("logs/audit", "Audit log", "logs.view"), ("logs/settings", "Log routing", "admin.config")]),
    ("Bot", [("embeds", "Embed builder", "embeds.manage"), ("welcome", "Welcome messages", "embeds.manage"),
             ("commands", "Slash commands", "dashboard.view"), ("bot/appearance", "Appearance", "admin.config")]),
    ("AI system", [("ai", "Providers", "admin.ai"), ("ai/personality", "Dispatcher personality", "admin.config"),
                   ("ai/usage", "AI usage", "admin.ai")]),
    ("Settings", [("settings", "General", "admin.config"), ("settings/permissions", "Permissions", "admin.permissions"),
                  ("settings/notifications", "Notifications", "admin.config"), ("settings/integrations", "Integrations", "dashboard.view"),
                  ("settings/security", "Security", "dashboard.view")]),
]


def nav_allowed(perm, perms: set[str]) -> bool:
    return any(p in perms for p in perm) if isinstance(perm, tuple) else perm in perms


# v5.0 routes are registered first so their specific paths win over older patterns like /moderation/{n}.
from .api_v5 import PlatformApi  # noqa: E402


class Api(PlatformApi):
    def __init__(self, bot):
        self.bot = bot
        self._compiled = [(m, _compile(p), getattr(self, h), perm) for m, p, h, perm in ROUTES]

    @property
    def c(self):
        return self.bot.community

    @property
    def core(self):
        return self.bot.core

    async def dispatch(self, method: str, path: str, ctx: Ctx) -> Any:
        allowed_methods = []
        for m, rx, handler, perm in self._compiled:
            match = rx.match(path)
            if not match:
                continue
            if m != method:
                allowed_methods.append(m)
                continue
            if perm:
                ctx.need(perm)
            ctx.params = match.groupdict()
            try:
                return await handler(ctx)
            except (ServiceError, ValidationError) as e:
                raise ApiError(400, str(e)) from e
            except KeyError as e:
                raise ApiError(400, f"Missing field: {e}") from e
        if allowed_methods:
            raise ApiError(405, "Method not allowed.")
        raise ApiError(404, "Not found.")

    # ------------------------------------------------------------------ Discord helpers
    def _guild(self, gid: int):
        return self.bot.get_guild(gid) if hasattr(self.bot, "get_guild") else None

    def channels(self, gid: int) -> list[dict]:
        g = self._guild(gid)
        if g is None:
            return []
        out = []
        for ch in getattr(g, "text_channels", []):
            out.append({"id": str(ch.id), "name": ch.name, "category": ch.category.name if ch.category else ""})
        return out

    def roles(self, gid: int) -> list[dict]:
        g = self._guild(gid)
        if g is None:
            return []
        me = g.me
        return [{"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}" if r.color.value else None,
                 "managed": r.managed, "assignable": (r < me.top_role) and not r.managed}
                for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default()]

    def _check_channel(self, gid: int, channel_id: Any) -> Optional[int]:
        cid = _opt_int(channel_id)
        if cid is None:
            return None
        if self._guild(gid) is not None and str(cid) not in {c["id"] for c in self.channels(gid)}:
            raise ApiError(400, "That channel is not in this server.")
        return cid

    def _check_role(self, gid: int, role_id: Any) -> Optional[int]:
        rid = _opt_int(role_id)
        if rid is None:
            return None
        if self._guild(gid) is not None and str(rid) not in {r["id"] for r in self.roles(gid)}:
            raise ApiError(400, "That role is not in this server.")
        return rid

    # ================================================================== shell
    @route("GET", "/meta", "dashboard.view")
    async def meta(self, ctx: Ctx):
        g = self._guild(ctx.guild_id)
        nav = [{"group": grp, "items": [{"path": p, "label": lbl} for p, lbl, perm in items if nav_allowed(perm, ctx.perms)]} for grp, items in NAV]
        return {"guild": {"id": str(ctx.guild_id), "name": g.name if g else str(ctx.guild_id),
                          "icon": g.icon.url if g is not None and g.icon else None},
                "nav": [n for n in nav if n["items"]], "permissions": sorted(ctx.perms),
                "channels": self.channels(ctx.guild_id), "roles": self.roles(ctx.guild_id),
                "user": {"id": str(ctx.user_id), "name": ctx.username}}

    @route("GET", "/prefs", "dashboard.view")
    async def prefs(self, ctx: Ctx):
        row = await self.bot.db.fetchone("SELECT prefs FROM dashboard_prefs WHERE guild_id = ? AND user_id = ?", (ctx.guild_id, ctx.user_id))
        return loads(row["prefs"], {}) if row else {}

    @route("PUT", "/prefs", "dashboard.view")
    async def prefs_save(self, ctx: Ctx):
        allowed = {"widgets", "hidden", "recent", "range", "onboarded"}
        prefs = {k: v for k, v in ctx.body.items() if k in allowed}
        raw = dumps(prefs)
        if len(raw) > 4000:
            raise ApiError(400, "Preferences are too large.")
        await self.bot.db.execute("INSERT INTO dashboard_prefs(guild_id, user_id, prefs, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) "
                                  "DO UPDATE SET prefs = excluded.prefs, updated_at = excluded.updated_at",
                                  (ctx.guild_id, ctx.user_id, raw, iso_now()))
        return prefs

    # ================================================================== overview
    @route("GET", "/overview", "dashboard.view")
    async def overview(self, ctx: Ctx):
        g, gid, c = self._guild(ctx.guild_id), ctx.guild_id, self.c
        today = utcnow().strftime("%Y-%m-%d")
        counts = await c.members.counts(gid)
        online = None
        if g is not None and getattr(g, "members", None) and hasattr(g, "chunked") and g.chunked:
            try:
                online = sum(1 for m in g.members if str(getattr(m, "status", "offline")) != "offline" and not m.bot)
            except Exception:
                online = None
        t0 = time.perf_counter()
        db_ok = True
        try:
            await self.bot.db.fetchval("SELECT 1")
        except Exception:
            db_ok = False
        db_ms = round((time.perf_counter() - t0) * 1000, 1)
        sync = await self.bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (gid,))
        ai = self.bot.ai.active_provider(gid) if getattr(self.bot, "ai", None) else None
        providers = await self.bot.ai.guild_providers(gid) if getattr(self.bot, "ai", None) else []
        active_incidents = await self.core.incidents.list_active(gid)
        av = await self.core.units.availability_summary(gid)
        up = (utcnow() - (parse_iso(getattr(self.bot, "started_at_iso", None)) or utcnow())).total_seconds()
        lat = getattr(self.bot, "latency", None)
        latency_ms = round(lat * 1000) if isinstance(lat, (int, float)) and lat == lat and lat != float("inf") else None
        out = {
            "members": {"total": g.member_count if g is not None else counts["total"], "tracked": counts["total"], "online": online,
                        "verified": counts["verified"]},
            "staff": {"total": await c.staff.count(gid), "on_shift": len(await c.shifts.active(gid))},
            "dispatch": {"active_incidents": len(active_incidents),
                         "critical": sum(1 for i in active_incidents if (i.get("priority") or i.get("ai_priority")) == "critical"),
                         "units": av, "dispatchers_on_shift": await self.bot.db.fetchval(
                             "SELECT COUNT(*) FROM shifts WHERE guild_id = ? AND ended_at IS NULL AND shift_type = 'Dispatch'", (gid,), 0)},
            "sessions": {"active": len(await c.sessions.list(gid, "active")), "upcoming": len(await c.sessions.list(gid, "scheduled"))},
            "applications": await c.applications.counts(gid),
            "verification": {"pending": len(await c.verification.list_pending(gid)), "today": (await c.verification.stats(gid, today))["verified"]},
            "moderation": await c.moderation.counts(gid, today),
            "automations": {"active": await c.automation.active_count(gid)},
            "system": {"uptime_s": int(up), "latency_ms": latency_ms,
                       "database": {"ok": db_ok, "ms": db_ms},
                       "erlc": {"connected": bool(sync and sync["connected"]), "server": sync["server_name"] if sync else None,
                                "players": sync["current_players"] if sync else None, "max_players": sync["max_players"] if sync else None,
                                "last_error": sync["last_error"] if sync and not sync["connected"] else None,
                                "configured": bool(sync)},
                       "ai": {"active": ai, "configured": sum(1 for p in providers if p["configured"] and p["enabled"])}},
            "recent": {
                "moderation": [dict(r, target_id=str(r["target_id"])) for r in row_dicts(await self.bot.db.fetchall(
                    "SELECT number, action, target_id, target_name, reason, created_at FROM mod_cases WHERE guild_id = ? ORDER BY id DESC LIMIT 6", (gid,)))]
                if "mod.view" in ctx.perms else None,
                "audit": [dict(r, actor_id=_sid(r["actor_id"])) for r in await self.c.audit.recent(gid, 8)] if "logs.view" in ctx.perms else None,
                "activity": (await self.bot.activity_feed(gid))[-10:][::-1] if hasattr(self.bot, "activity_feed") else [],
            },
            "notifications": await c.notifications.list_for(gid, ctx.user_id, ctx.perms, limit=5, unread_only=True),
        }
        # v5.0 additions
        out["unread"] = await c.notifications.unread_count(gid, ctx.user_id, ctx.perms)
        out["bolos"] = {"active": await self.bot.db.fetchval("SELECT COUNT(*) FROM bolos WHERE guild_id = ? AND status = 'active'", (gid,), 0),
                        "urgent": await self.bot.db.fetchval("SELECT COUNT(*) FROM bolos WHERE guild_id = ? AND status = 'active' AND "
                                                             "priority IN ('high', 'critical')", (gid,), 0)} if "bolo.view" in ctx.perms else None
        out["health"] = await self.platform_health(ctx)
        out["trend"] = await self.activity_trend(gid)
        out["recent"]["in_game"] = [self.c.erlc_moderation._rec(r) for r in await self.bot.db.fetchall(
            "SELECT * FROM erlc_mod_records WHERE guild_id = ? ORDER BY id DESC LIMIT 6", (gid,))] if "erlc.view" in ctx.perms else None
        from core.activity import visible_categories
        cats = visible_categories(ctx.perms) or ["-"]
        out["recent"]["feed"] = await self.humanize(gid, row_dicts(await self.bot.db.fetchall(
            f"SELECT id, at, category, title, body, link FROM activity_events WHERE guild_id = ? AND category IN ({','.join('?' * len(cats))}) "
            "ORDER BY id DESC LIMIT 12", (gid, *cats))))
        await self.humanize(gid, out["notifications"])
        out["staff"]["on_leave"] = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM leave_requests WHERE guild_id = ? AND status = 'approved' AND starts_on <= ? AND ends_on >= ?",
            (gid, today, today), 0)
        out["staff"]["pending_leave"] = await self.bot.db.fetchval("SELECT COUNT(*) FROM leave_requests WHERE guild_id = ? AND status = 'pending'",
                                                                   (gid,), 0)
        out["me"] = {"shift": await c.shifts.current(gid, ctx.user_id)} if "shift.self" in ctx.perms else None
        if out["me"] and out["me"]["shift"]:
            out["me"]["shift"]["user_id"] = str(out["me"]["shift"]["user_id"])
        return out

    @route("GET", "/analytics", "analytics.view")
    async def analytics(self, ctx: Ctx):
        return await self.c.analytics.summary(ctx.guild_id, ctx.q("range", "7d"), ctx.q("start") or None, ctx.q("end") or None)

    # ================================================================== dispatch
    async def _incident_out(self, inc: dict, with_units: bool = True) -> dict:
        d = dict(inc)
        d["recommended_departments"] = loads(d.get("recommended_departments"), [])
        d.pop("assessment_json", None)
        for k in ("created_by", "closed_by", "priority_approved_by", "dispatcher_id"):
            d[k] = _sid(d.get(k))
        d["effective_priority"] = d.get("priority") or d.get("ai_priority")
        if with_units:
            d["units"] = [{"id": u["id"], "callsign": u["callsign"], "status_code": u.get("status_code")}
                          for u in await self.core.incidents.assigned_units(inc["id"])]
        return d

    async def _inc(self, ctx: Ctx) -> dict:
        inc = await self.core.incidents.get_by_number(ctx.guild_id, as_int(ctx.params["n"], "Incident number"))
        if not inc:
            raise ApiError(404, "Incident not found.")
        return inc

    @route("GET", "/dispatch", "incident.view")
    async def dispatch_board(self, ctx: Ctx):
        gid = ctx.guild_id
        active = [await self._incident_out(i) for i in await self.core.incidents.list_active(gid)]
        units = await self.core.units.list_units(gid, include_off="all" == ctx.q("units"))
        return {"incidents": active, "units": units, "status_codes": await self.core.units.status_codes(gid),
                "departments": await self.core.units.departments(gid),
                "workflow_statuses": await self._workflow_statuses(gid),
                "backups": [dict(b) for b in await self.core.assignments.open_backup_requests(gid)],
                "bolos": await self.core.bolos.active(gid),
                "priorities": [{"value": p, "label": PRIORITY_LABEL[p]} for p in PRIORITIES]}

    async def _workflow_statuses(self, gid: int) -> list[str]:
        return await self.bot.db.get_config(gid, "incident_workflow_statuses", None) or \
            ["New", "Pending", "Assigned", "In Progress", "Awaiting Update", "Resolved"]

    @route("GET", "/incidents", "incident.view")
    async def incidents_history(self, ctx: Ctx):
        where, params = ["guild_id = ?"], [ctx.guild_id]
        status = ctx.q("status")
        if status in {"pending", "active", "closed"}:
            where.append("status = ?")
            params.append(status)
        if ctx.q("priority") in PRIORITIES:
            where.append("COALESCE(priority, ai_priority) = ?")
            params.append(ctx.q("priority"))
        if ctx.q("source") in {"erlc", "discord", "manual", "test"}:
            where.append("source = ?")
            params.append(ctx.q("source"))
        if q := ctx.q("q").strip():
            where.append("(nature LIKE ? OR location LIKE ? OR description LIKE ? OR CAST(number AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", f"%{q}%", q.lstrip("#")]
        if ctx.q("since"):
            where.append("created_at >= ?")
            params.append(ctx.q("since"))
        base = " FROM incidents WHERE " + " AND ".join(where)
        pg = await paginate_sql(self.bot.db, "SELECT *" + base + " ORDER BY id DESC", "SELECT COUNT(*)" + base, params, ctx.qi("page"), 25)
        pg.items = [await self._incident_out(i, with_units=False) for i in pg.items]
        return pg.as_dict()

    @route("GET", "/incidents/{n}", "incident.view")
    async def incident_detail(self, ctx: Ctx):
        inc = await self._inc(ctx)
        hist = row_dicts(await self.bot.db.fetchall("SELECT * FROM incident_history WHERE incident_id = ? ORDER BY id DESC LIMIT 200", (inc["id"],)))
        for h in hist:
            h["detail"] = loads(h.get("detail"), {})
            h["actor_id"] = _sid(h.get("actor_id"))
        return {"incident": await self._incident_out(inc), "history": hist}

    @route("POST", "/incidents", "incident.manual_create")
    async def incident_create(self, ctx: Ctx):
        inc = await self.core.create_manual_incident(ctx.guild_id, ctx.user_id, nature=clean_text(ctx.b("nature"), 120, required=True, field="Nature"),
                                                     location=clean_text(ctx.b("location"), 200, required=True, field="Location"),
                                                     description=clean_text(ctx.b("description"), 1500), reporter=ctx.username)
        if ctx.b("category"):
            await self.core.incidents.update_fields(inc["id"], ctx.user_id, category=clean_text(ctx.b("category"), 60))
        return await self._incident_out(await self.core.incidents.get(inc["id"]))

    @route("PATCH", "/incidents/{n}", "incident.update")
    async def incident_update(self, ctx: Ctx):
        inc = await self._inc(ctx)
        fields = {k: clean_text(ctx.b(k), 1500 if k == "description" else 200) or None
                  for k in ("nature", "location", "description", "category") if k in ctx.body}
        if fields:
            inc = await self.core.incidents.update_fields(inc["id"], ctx.user_id, **fields)
        extra = {}
        if "workflow_status" in ctx.body:
            ws = ctx.b("workflow_status") or None
            if ws and ws not in await self._workflow_statuses(ctx.guild_id):
                raise ApiError(400, "Unknown status.")
            extra["workflow_status"] = ws
        if "dispatcher_id" in ctx.body:
            extra["dispatcher_id"] = _opt_int(ctx.b("dispatcher_id"))
        if extra:
            sets = ", ".join(f"{k} = ?" for k in extra)
            await self.bot.db.execute(f"UPDATE incidents SET {sets}, updated_at = ? WHERE id = ?", (*extra.values(), iso_now(), inc["id"]))
            await self.core.incidents.add_history(inc, "updated", ctx.user_id, after={k: str(v) for k, v in extra.items()})
            await self.bot.bus.publish("incident.updated", ctx.guild_id, incident_id=inc["id"], changes=list(extra))
        return await self._incident_out(await self.core.incidents.get(inc["id"]))

    @route("POST", "/incidents/{n}/note", "incident.note")
    async def incident_note(self, ctx: Ctx):
        inc = await self._inc(ctx)
        await self.core.incidents.add_note(inc["id"], ctx.user_id, clean_text(ctx.b("note"), 500, required=True, field="Note"), author=ctx.username)
        return {"ok": True}

    @route("POST", "/incidents/{n}/priority", "incident.priority_approve")
    async def incident_priority(self, ctx: Ctx):
        inc = await self._inc(ctx)
        p = ctx.b("priority")
        if p not in PRIORITIES:
            raise ApiError(400, "Unknown priority.")
        await self.core.incidents.set_priority(inc["id"], p, ctx.user_id, clean_text(ctx.b("reason"), 300) or None)
        return await self._incident_out(await self.core.incidents.get(inc["id"]))

    @route("POST", "/incidents/{n}/assign", "incident.attach_other")
    async def incident_assign(self, ctx: Ctx):
        inc = await self._inc(ctx)
        unit = await self.core.units.unit_by_callsign(ctx.guild_id, str(ctx.b("callsign") or ""))
        if not unit:
            raise ApiError(400, "Unit not found.")
        try:
            await self.core.assignments.attach(inc["id"], unit["id"], ctx.user_id)
        except Exception as e:
            if type(e).__name__ in {"IncidentError", "UnitError"}:
                raise ApiError(400, str(e)) from e
            raise
        return await self._incident_out(await self.core.incidents.get(inc["id"]))

    @route("POST", "/incidents/{n}/unassign", "incident.attach_other")
    async def incident_unassign(self, ctx: Ctx):
        inc = await self._inc(ctx)
        unit = await self.core.units.unit_by_callsign(ctx.guild_id, str(ctx.b("callsign") or ""))
        if not unit:
            raise ApiError(400, "Unit not found.")
        try:
            await self.core.assignments.detach(inc["id"], unit["id"], ctx.user_id, "cleared from dashboard")
        except Exception as e:
            if type(e).__name__ in {"IncidentError", "UnitError"}:
                raise ApiError(400, str(e)) from e
            raise
        return await self._incident_out(await self.core.incidents.get(inc["id"]))

    @route("POST", "/incidents/{n}/close", "incident.close")
    async def incident_close(self, ctx: Ctx):
        inc = await self._inc(ctx)
        if inc["status"] == "closed":
            raise ApiError(400, "That incident is already closed.")
        await self.core.incidents.close(inc["id"], ctx.user_id, clean_text(ctx.b("reason"), 300) or "Closed from the dashboard")
        return {"ok": True}

    @route("GET", "/units", "incident.view")
    async def units(self, ctx: Ctx):
        units = await self.core.units.list_units(ctx.guild_id, include_off=True)
        officers = row_dicts(await self.bot.db.fetchall(
            "SELECT o.discord_id, o.name, o.roblox_username, o.unit_id, d.short_code AS dept_code FROM officers o "
            "LEFT JOIN departments d ON d.id = o.department_id WHERE o.guild_id = ? AND o.active = 1 ORDER BY o.name", (ctx.guild_id,)))
        for o in officers:
            o["discord_id"] = str(o["discord_id"])
        hist = row_dicts(await self.bot.db.fetchall(
            "SELECT h.at, h.old_code, h.new_code, u.callsign FROM unit_status_history h JOIN units u ON u.id = h.unit_id "
            "WHERE h.guild_id = ? ORDER BY h.id DESC LIMIT 40", (ctx.guild_id,)))
        return {"units": units, "officers": officers, "history": hist, "status_codes": await self.core.units.status_codes(ctx.guild_id),
                "departments": await self.core.units.departments(ctx.guild_id)}

    @route("POST", "/units/register", "unit.manage_other")
    async def unit_register(self, ctx: Ctx):
        uid = as_int(ctx.b("user_id"), "Member")
        o = await self.core.units.register(ctx.guild_id, uid, ctx.b("name"), ctx.b("callsign"), ctx.b("department"),
                                           roblox_username=ctx.b("roblox_username"), actor_id=ctx.user_id)
        return {"officer": {**o, "discord_id": str(o["discord_id"])}}

    @route("POST", "/units/{uid}/status", "unit.manage_other")
    async def unit_status(self, ctx: Ctx):
        unit = await self.core.units.unit(as_int(ctx.params["uid"], "Unit"))
        if not unit or unit["guild_id"] != ctx.guild_id:
            raise ApiError(404, "Unit not found.")
        try:
            return await self.core.assignments.set_unit_status(unit["id"], str(ctx.b("code") or ""), ctx.user_id)
        except Exception as e:
            if type(e).__name__ == "UnitError":
                raise ApiError(400, str(e)) from e
            raise

    @route("DELETE", "/units/officers/{uid}", "unit.manage_other")
    async def unit_remove_officer(self, ctx: Ctx):
        ok = await self.core.units.deactivate_officer(ctx.guild_id, as_int(ctx.params["uid"]), ctx.user_id)
        if not ok:
            raise ApiError(404, "That member is not registered to a unit.")
        return {"ok": True}

    @route("GET", "/dispatch/settings", "admin.config")
    async def dispatch_settings(self, ctx: Ctx):
        from dispatch.core import DEFAULT_SETTINGS
        from dispatch.phrases import STYLES
        from database.models import DEPARTMENT_KINDS
        stored = await self.bot.db.get_config(ctx.guild_id, "dispatch_settings", {}) or {}
        return {"settings": {**DEFAULT_SETTINGS, **stored}, "styles": STYLES, "workflow_statuses": await self._workflow_statuses(ctx.guild_id),
                "departments": await self.core.units.departments(ctx.guild_id), "department_kinds": DEPARTMENT_KINDS,
                "status_codes": await self.core.units.status_codes(ctx.guild_id),
                "categories": await self.bot.db.get_config(ctx.guild_id, "custom_categories", []) or [],
                "priorities": [{"value": p, "label": PRIORITY_LABEL[p]} for p in PRIORITIES]}

    @route("PUT", "/dispatch/settings", "admin.config")
    async def dispatch_settings_save(self, ctx: Ctx):
        from dispatch.core import DEFAULT_SETTINGS
        from dispatch.phrases import STYLES
        changes = ctx.b("settings") or {}
        for k, v in changes.items():
            if k not in DEFAULT_SETTINGS:
                raise ApiError(400, f"Unknown setting {k}.")
            default = DEFAULT_SETTINGS[k]
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = max(0, min(86400, int(v)))
            elif k == "response_style" and v not in STYLES:
                raise ApiError(400, "Unknown response style.")
            elif k in {"priority_approval_threshold", "repeat_min_priority"} and v not in PRIORITIES:
                raise ApiError(400, "Unknown priority.")
            else:
                v = clean_text(str(v), 80)
            await self.core.set_setting(ctx.guild_id, k, v)
        if "workflow_statuses" in ctx.body:
            ws = [w for w in (clean_text(x, 30) for x in ctx.b("workflow_statuses") or []) if w][:12]
            if not ws:
                raise ApiError(400, "Keep at least one status.")
            await self.bot.db.set_config(ctx.guild_id, "incident_workflow_statuses", ws)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "dashboard.dispatch_settings", None, {"keys": sorted(changes)})
        return await self.dispatch_settings(ctx)

    @route("POST", "/dispatch/departments", "admin.config")
    async def department_add(self, ctx: Ctx):
        d = await self.core.units.add_department(ctx.guild_id, ctx.b("name"), ctx.b("code"), ctx.b("kind") or "other", ctx.user_id,
                                                 role_id=self._check_role(ctx.guild_id, ctx.b("role_id")))
        return d

    @route("DELETE", "/dispatch/departments/{code}", "admin.config")
    async def department_remove(self, ctx: Ctx):
        if not await self.core.units.remove_department(ctx.guild_id, ctx.params["code"], ctx.user_id):
            raise ApiError(404, "Department not found.")
        return {"ok": True}

    @route("POST", "/dispatch/status-codes", "admin.config")
    async def status_code_save(self, ctx: Ctx):
        if ctx.b("availability") not in {"available", "assigned", "unavailable", "off"}:
            raise ApiError(400, "Availability must be available, assigned, unavailable or off.")
        await self.core.units.save_status_code(ctx.guild_id, clean_text(ctx.b("code"), 12, required=True, field="Code").upper(),
                                               clean_text(ctx.b("label"), 40, required=True, field="Label"), ctx.b("availability"), ctx.user_id)
        return {"ok": True}

    @route("DELETE", "/dispatch/status-codes/{code}", "admin.config")
    async def status_code_remove(self, ctx: Ctx):
        if not await self.core.units.remove_status_code(ctx.guild_id, ctx.params["code"], ctx.user_id):
            raise ApiError(400, "That status code can't be removed (it may be in use or required).")
        return {"ok": True}

    # ================================================================== members & verification
    @route("GET", "/members", "members.view")
    async def members(self, ctx: Ctx):
        return await self.c.members.directory(ctx.guild_id, q=ctx.q("q"), filter=ctx.q("filter", "all"), department=ctx.q("department"),
                                              page=ctx.qi("page"), per_page=25)

    @route("GET", "/members/{uid}", "members.view")
    async def member(self, ctx: Ctx):
        p = await self.c.members.profile(ctx.guild_id, as_int(ctx.params["uid"]), include_notes="members.notes" in ctx.perms,
                                         include_cases="mod.view" in ctx.perms)
        p["member"] = {**p["member"], "user_id": str(p["member"].get("user_id"))}
        if p.get("notes"):
            for n in p["notes"]:
                n["author_id"] = str(n["author_id"])
        return p

    @route("POST", "/members/{uid}/notes", "members.notes")
    async def member_note(self, ctx: Ctx):
        await self.c.members.add_note(ctx.guild_id, as_int(ctx.params["uid"]), ctx.user_id, ctx.b("note"))
        return {"ok": True}

    @route("DELETE", "/members/notes/{nid}", "members.notes")
    async def member_note_delete(self, ctx: Ctx):
        if not await self.c.members.delete_note(ctx.guild_id, as_int(ctx.params["nid"]), ctx.user_id):
            raise ApiError(404, "Note not found.")
        return {"ok": True}

    @route("GET", "/verification", "verification.manage")
    async def verification(self, ctx: Ctx):
        from community.verification import NICKNAME_FORMATS
        v = self.c.verification
        s = await v.settings(ctx.guild_id)
        return {"settings": {k: (_sid(val) if k.endswith("_id") else val) for k, val in s.items()},
                "nickname_formats": NICKNAME_FORMATS, "stats": await v.stats(ctx.guild_id, utcnow().strftime("%Y-%m-%d")),
                "pending": await v.list_pending(ctx.guild_id), "links": await v.list_links(ctx.guild_id, ctx.q("q"), ctx.qi("page")),
                "oauth": {"configured": v.oauth_available(), "public_url": bool(self.bot.settings.dashboard_public_url)},
                "external": await v.external_status(ctx.guild_id)}

    @route("POST", "/verification/keys/{provider}", "admin.config")
    async def verification_key(self, ctx: Ctx):
        """Write-only: Bloxlink / RoVer server API keys are encrypted and never sent back."""
        await self.c.verification.set_external_key(ctx.guild_id, ctx.params["provider"], str(ctx.b("key") or ""), ctx.user_id)
        return {"ok": True}

    @route("DELETE", "/verification/keys/{provider}", "admin.config")
    async def verification_key_delete(self, ctx: Ctx):
        await self.c.verification.set_external_key(ctx.guild_id, ctx.params["provider"], None, ctx.user_id)
        return {"ok": True}

    @route("POST", "/verification/keys/{provider}/test", "admin.config")
    async def verification_key_test(self, ctx: Ctx):
        return await self.c.verification.test_external(ctx.guild_id, ctx.params["provider"], ctx.user_id)

    @route("PUT", "/verification/settings", "admin.config")
    async def verification_settings(self, ctx: Ctx):
        changes = dict(ctx.body)
        for k in ("verified_role_id", "unverified_role_id"):
            if k in changes:
                changes[k] = self._check_role(ctx.guild_id, changes[k])
        if "panel_channel_id" in changes:
            changes["panel_channel_id"] = self._check_channel(ctx.guild_id, changes["panel_channel_id"])
        s = await self.c.verification.update_settings(ctx.guild_id, changes, ctx.user_id)
        return {k: (_sid(val) if k.endswith("_id") else val) for k, val in s.items()}

    @route("POST", "/verification/link", "verification.manage")
    async def verification_link(self, ctx: Ctx):
        return await self.c.verification.manual_link(ctx.guild_id, as_int(ctx.b("user_id"), "Member"), str(ctx.b("roblox_username") or ""),
                                                     ctx.user_id, ctx.b("reason") or "")

    @route("DELETE", "/verification/{uid}", "verification.manage")
    async def verification_unlink(self, ctx: Ctx):
        if not await self.c.verification.unlink(ctx.guild_id, as_int(ctx.params["uid"]), ctx.user_id, ctx.q("reason")):
            raise ApiError(404, "That member is not verified.")
        return {"ok": True}

    @route("POST", "/verification/{uid}/sync", "verification.manage")
    async def verification_sync(self, ctx: Ctx):
        return {"problems": await self.c.verification.sync_roles(ctx.guild_id, as_int(ctx.params["uid"]))}

    @route("GET", "/verification/history", "verification.manage")
    async def verification_history(self, ctx: Ctx):
        return await self.c.verification.history(ctx.guild_id, _opt_int(ctx.q("user_id")), ctx.qi("page"))

    # ================================================================== applications
    @route("GET", "/applications", "application.review")
    async def applications(self, ctx: Ctx):
        return await self.c.applications.list(ctx.guild_id, status=ctx.q("status", "open"), form_id=_opt_int(ctx.q("form")),
                                              q=ctx.q("q"), page=ctx.qi("page"))

    @route("GET", "/applications/meta", "application.review")
    async def applications_meta(self, ctx: Ctx):
        from community.applications import FORM_KINDS, PRESETS, QUESTION_TYPES, STATUSES
        return {"question_types": QUESTION_TYPES, "kinds": FORM_KINDS, "statuses": STATUSES, "presets": list(PRESETS),
                "settings": await self.c.applications.settings(ctx.guild_id)}

    @route("GET", "/applications/forms", "application.review")
    async def forms(self, ctx: Ctx):
        return {"forms": await self.c.applications.forms(ctx.guild_id)}

    @route("POST", "/applications/forms", "application.manage")
    async def form_create(self, ctx: Ctx):
        return await self.c.applications.save_form(ctx.guild_id, self._form_body(ctx), ctx.user_id)

    @route("PUT", "/applications/forms/{fid}", "application.manage")
    async def form_update(self, ctx: Ctx):
        return await self.c.applications.save_form(ctx.guild_id, self._form_body(ctx), ctx.user_id, form_id=as_int(ctx.params["fid"]))

    def _form_body(self, ctx: Ctx) -> dict:
        b = dict(ctx.body)
        b["approve_role_id"] = self._check_role(ctx.guild_id, b.get("approve_role_id"))
        b["review_channel_id"] = self._check_channel(ctx.guild_id, b.get("review_channel_id"))
        for k in ("submit_roles", "reviewer_roles"):
            b[k] = [self._check_role(ctx.guild_id, r) for r in b.get(k) or []]
        return b

    @route("DELETE", "/applications/forms/{fid}", "application.manage")
    async def form_delete(self, ctx: Ctx):
        if not await self.c.applications.delete_form(ctx.guild_id, as_int(ctx.params["fid"]), ctx.user_id):
            raise ApiError(404, "Form not found.")
        return {"ok": True}

    @route("POST", "/applications/forms/preset", "application.manage")
    async def form_preset(self, ctx: Ctx):
        return await self.c.applications.create_from_preset(ctx.guild_id, str(ctx.b("preset") or ""), ctx.user_id)

    @route("PUT", "/applications/settings", "application.manage")
    async def applications_settings(self, ctx: Ctx):
        from community.applications import DEFAULTS
        changes = {}
        if "dm_results" in ctx.body:
            changes["dm_results"] = bool(ctx.b("dm_results"))
        if "archive_after_days" in ctx.body:
            changes["archive_after_days"] = max(0, min(365, int(ctx.b("archive_after_days") or 0)))
        return await self.c.update_settings(ctx.guild_id, "application_settings", DEFAULTS, changes, ctx.user_id)

    @route("GET", "/applications/{n}", "application.review")
    async def application(self, ctx: Ctx):
        app = await self.c.applications.get(ctx.guild_id, as_int(ctx.params["n"]))
        if not app:
            raise ApiError(404, "Application not found.")
        for n in app["notes"]:
            n["author_id"] = str(n["author_id"])
        app["reviewer_id"] = _sid(app.get("reviewer_id"))
        return app

    @route("POST", "/applications/{n}/status", "application.review")
    async def application_status(self, ctx: Ctx):
        return await self.c.applications.set_status(ctx.guild_id, as_int(ctx.params["n"]), str(ctx.b("status") or ""), ctx.user_id,
                                                    ctx.b("reason") or "")

    @route("POST", "/applications/{n}/note", "application.review")
    async def application_note(self, ctx: Ctx):
        await self.c.applications.add_note(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, ctx.b("note"))
        return {"ok": True}

    # ================================================================== sessions
    @route("GET", "/sessions", "session.view")
    async def sessions(self, ctx: Ctx):
        from community.sessions import DEFAULTS
        s = await self.c.sessions.settings(ctx.guild_id)
        return {"sessions": await self.c.sessions.list(ctx.guild_id, ctx.q("status", "all"), 100),
                "settings": {k: (_sid(v) if k.endswith("_id") else v) for k, v in s.items()}, "defaults": DEFAULTS}

    @route("POST", "/sessions", "session.manage")
    async def session_create(self, ctx: Ctx):
        return await self.c.sessions.create(ctx.guild_id, ctx.user_id, name=ctx.b("name"), description=ctx.b("description") or "",
                                            scheduled_for=ctx.b("scheduled_for") or None, host_id=_opt_int(ctx.b("host_id")),
                                            department=ctx.b("department") or "", reminder_minutes=_opt_int(ctx.b("reminder_minutes")),
                                            channel_id=self._check_channel(ctx.guild_id, ctx.b("channel_id")), announce=bool(ctx.b("announce", True)))

    @route("GET", "/sessions/{n}", "session.view")
    async def session(self, ctx: Ctx):
        s = await self.c.sessions.get(ctx.guild_id, as_int(ctx.params["n"]))
        if not s:
            raise ApiError(404, "Session not found.")
        return s

    @route("POST", "/sessions/{n}/start", "session.manage")
    async def session_start(self, ctx: Ctx):
        return await self.c.sessions.start(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id)

    @route("POST", "/sessions/{n}/end", "session.manage")
    async def session_end(self, ctx: Ctx):
        return await self.c.sessions.end(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, ctx.b("notes") or "")

    @route("PUT", "/sessions/settings", "session.manage")
    async def sessions_settings(self, ctx: Ctx):
        changes = dict(ctx.body)
        if "announce_channel_id" in changes:
            changes["announce_channel_id"] = self._check_channel(ctx.guild_id, changes["announce_channel_id"])
        if "ping_role_id" in changes:
            changes["ping_role_id"] = self._check_role(ctx.guild_id, changes["ping_role_id"])
        return await self.c.sessions.update_settings(ctx.guild_id, changes, ctx.user_id)

    # ================================================================== staff & shifts
    @route("GET", "/staff", "staff.view")
    async def staff(self, ctx: Ctx):
        data = await self.c.staff.list(ctx.guild_id, status=ctx.q("status", "current"), q=ctx.q("q"), department=ctx.q("department"),
                                       page=ctx.qi("page"), per_page=50)
        data["settings"] = await self.c.staff.settings(ctx.guild_id)
        return data

    @route("GET", "/staff/settings", "staff.manage")
    async def staff_settings(self, ctx: Ctx):
        st = await self.c.staff.settings(ctx.guild_id)
        sh = await self.c.shifts.settings(ctx.guild_id)
        st["ranks"] = [{**r, "role_id": _sid(r.get("role_id"))} for r in st["ranks"]]
        return {"staff": {**st, "leave_role_id": _sid(st.get("leave_role_id"))}, "shifts": {**sh, "on_duty_role_id": _sid(sh.get("on_duty_role_id"))}}

    @route("PUT", "/staff/settings", "staff.manage")
    async def staff_settings_save(self, ctx: Ctx):
        if "staff" in ctx.body:
            st = dict(ctx.b("staff") or {})
            if "ranks" in st:
                st["ranks"] = [{**r, "role_id": self._check_role(ctx.guild_id, r.get("role_id"))} for r in st["ranks"] or []]
            if "leave_role_id" in st:
                st["leave_role_id"] = self._check_role(ctx.guild_id, st["leave_role_id"])
            await self.c.staff.update_settings(ctx.guild_id, st, ctx.user_id)
        if "shifts" in ctx.body:
            sh = dict(ctx.b("shifts") or {})
            if "on_duty_role_id" in sh:
                sh["on_duty_role_id"] = self._check_role(ctx.guild_id, sh["on_duty_role_id"])
            await self.c.shifts.update_settings(ctx.guild_id, sh, ctx.user_id)
        return await self.staff_settings(ctx)

    @route("GET", "/staff/leave", "staff.leave_review")
    async def leave(self, ctx: Ctx):
        return {"items": await self.c.staff.leave_list(ctx.guild_id, ctx.q("status"))}

    @route("POST", "/staff/leave/{lid}", "staff.leave_review")
    async def leave_review(self, ctx: Ctx):
        return await self.c.staff.review_leave(ctx.guild_id, as_int(ctx.params["lid"]), bool(ctx.b("approve")), ctx.user_id)

    @route("POST", "/staff", "staff.manage")
    async def staff_add(self, ctx: Ctx):
        return await self.c.staff.appoint(ctx.guild_id, as_int(ctx.b("user_id"), "Member"), ctx.b("rank") or "", ctx.user_id,
                                          department=ctx.b("department") or "", reason=ctx.b("reason") or "")

    @route("GET", "/staff/{uid}", "staff.view")
    async def staff_profile(self, ctx: Ctx):
        p = await self.c.staff.profile(ctx.guild_id, as_int(ctx.params["uid"]), include_notes="staff.notes" in ctx.perms)
        for n in p.get("notes") or []:
            n["author_id"] = str(n["author_id"])
        for h in p["history"]:
            h["actor_id"] = _sid(h.get("actor_id"))
        return p

    @route("POST", "/staff/{uid}/rank", "staff.manage")
    async def staff_rank(self, ctx: Ctx):
        return await self.c.staff.change_rank(ctx.guild_id, as_int(ctx.params["uid"]), ctx.user_id, direction=ctx.b("direction"),
                                              rank=ctx.b("rank") or None, reason=ctx.b("reason") or "")

    @route("POST", "/staff/{uid}/department", "staff.manage")
    async def staff_department(self, ctx: Ctx):
        return await self.c.staff.set_department(ctx.guild_id, as_int(ctx.params["uid"]), ctx.b("department") or "", ctx.user_id)

    @route("DELETE", "/staff/{uid}", "staff.manage")
    async def staff_remove(self, ctx: Ctx):
        return await self.c.staff.remove(ctx.guild_id, as_int(ctx.params["uid"]), ctx.user_id, ctx.q("reason"))

    @route("POST", "/staff/{uid}/notes", "staff.notes")
    async def staff_note(self, ctx: Ctx):
        await self.c.staff.add_note(ctx.guild_id, as_int(ctx.params["uid"]), ctx.user_id, ctx.b("note"))
        return {"ok": True}

    @route("GET", "/shifts", "shift.self")
    async def shifts(self, ctx: Ctx):
        sh = self.c.shifts
        mine = await sh.summary(ctx.guild_id, ctx.user_id)
        out = {"me": mine, "types": (await sh.settings(ctx.guild_id))["types"]}
        if "staff.view" in ctx.perms or "shift.manage" in ctx.perms:
            since = ctx.q("since") or (utcnow().strftime("%Y-%m-%d"))
            out["active"] = await sh.active(ctx.guild_id)
            out["history"] = await sh.history(ctx.guild_id, user_id=_opt_int(ctx.q("user_id")), page=ctx.qi("page"))
            import datetime as dt
            week = (utcnow() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
            out["leaderboard"] = await sh.leaderboard(ctx.guild_id, week)
            out["since"] = since
        else:
            out["history"] = await sh.history(ctx.guild_id, user_id=ctx.user_id, page=ctx.qi("page"))
        return out

    @route("POST", "/shifts/start", "shift.self")
    async def shift_start(self, ctx: Ctx):
        return await self.c.shifts.start(ctx.guild_id, ctx.user_id, ctx.b("type") or "")

    @route("POST", "/shifts/end", "shift.self")
    async def shift_end(self, ctx: Ctx):
        target = _opt_int(ctx.b("user_id")) or ctx.user_id
        if target != ctx.user_id:
            ctx.need("shift.manage")
        return await self.c.shifts.end(ctx.guild_id, target, ctx.user_id, ctx.b("notes") or "")

    # ================================================================== moderation
    @route("GET", "/moderation", "mod.view")
    async def cases(self, ctx: Ctx):
        from community.moderation import ACTIONS
        data = await self.c.moderation.list(ctx.guild_id, action=ctx.q("action"), status=ctx.q("status"), q=ctx.q("q"),
                                            target_id=_opt_int(ctx.q("target_id")), moderator_id=_opt_int(ctx.q("moderator_id")),
                                            since=ctx.q("since"), until=ctx.q("until"), page=ctx.qi("page"))
        data["actions"] = ACTIONS
        return data

    @route("POST", "/moderation", "mod.view")
    async def case_create(self, ctx: Ctx):
        action = str(ctx.b("action") or "")
        need = {"warn": "mod.warn", "note": "mod.warn", "timeout": "mod.timeout", "kick": "mod.kick", "ban": "mod.ban", "unban": "mod.ban"}
        if action not in need:
            raise ApiError(400, "Unknown action.")
        ctx.need(need[action])
        if action in {"kick", "ban"} and ctx.b("confirm") is not True:
            raise ApiError(400, "Confirm this action first.")
        return await self.c.moderation.create(ctx.guild_id, action, as_int(ctx.b("user_id"), "Member"), ctx.user_id,
                                              reason=ctx.b("reason") or "", evidence=ctx.b("evidence") or "", duration=ctx.b("duration"),
                                              source="dashboard", delete_message_days=int(ctx.b("delete_message_days") or 0))

    @route("GET", "/moderation/reports", "mod.reports_manage")
    async def reports(self, ctx: Ctx):
        return await self.c.moderation.reports(ctx.guild_id, ctx.q("status", "open"), ctx.qi("page"))

    @route("POST", "/moderation/reports/{n}", "mod.reports_manage")
    async def report_resolve(self, ctx: Ctx):
        r = await self.c.moderation.resolve_report(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, str(ctx.b("status") or ""),
                                                   ctx.b("resolution") or "", _opt_int(ctx.b("case_number")))
        return {**r, "reporter_id": str(r["reporter_id"]), "target_id": str(r["target_id"])}

    @route("GET", "/moderation/settings", "admin.config")
    async def moderation_settings(self, ctx: Ctx):
        return await self.c.moderation.settings(ctx.guild_id)

    @route("PUT", "/moderation/settings", "admin.config")
    async def moderation_settings_save(self, ctx: Ctx):
        return await self.c.moderation.update_settings(ctx.guild_id, ctx.body, ctx.user_id)

    @route("GET", "/moderation/{n}", "mod.view")
    async def case(self, ctx: Ctx):
        c = await self.c.moderation.get(ctx.guild_id, as_int(ctx.params["n"]))
        if not c:
            raise ApiError(404, "Case not found.")
        return c

    @route("POST", "/moderation/{n}/revoke", "mod.timeout")
    async def case_revoke(self, ctx: Ctx):
        return await self.c.moderation.revoke(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, ctx.b("reason") or "")

    @route("PATCH", "/moderation/{n}", "mod.warn")
    async def case_edit(self, ctx: Ctx):
        return await self.c.moderation.edit(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, reason=ctx.b("reason"), evidence=ctx.b("evidence"))

    # ================================================================== automation
    @route("GET", "/automation", "automation.manage")
    async def automations(self, ctx: Ctx):
        from community.automation import ACTIONS, OPERATORS, TEMPLATES, TRIGGERS
        return {"automations": await self.c.automation.list(ctx.guild_id), "runs": await self.c.automation.runs(ctx.guild_id, limit=40),
                "triggers": {k: {"label": v[0], "fields": v[1]} for k, v in TRIGGERS.items()},
                "actions": {k: {"label": v[0], "fields": v[1]} for k, v in ACTIONS.items()}, "operators": OPERATORS, "templates": TEMPLATES}

    @route("POST", "/automation", "automation.manage")
    async def automation_create(self, ctx: Ctx):
        return await self.c.automation.save(ctx.guild_id, self._automation_body(ctx), ctx.user_id)

    @route("GET", "/automation/schedules", "automation.manage")
    async def schedules(self, ctx: Ctx):
        return {"schedules": await self.c.automation.schedules(ctx.guild_id), "templates": [
            {"id": t["id"], "name": t["name"]} for t in await self.c.embeds.list(ctx.guild_id)]}

    @route("POST", "/automation/schedules", "automation.manage")
    async def schedule_create(self, ctx: Ctx):
        b = dict(ctx.body)
        b["channel_id"] = self._check_channel(ctx.guild_id, b.get("channel_id"))
        return await self.c.automation.save_schedule(ctx.guild_id, b, ctx.user_id)

    @route("PUT", "/automation/schedules/{sid}", "automation.manage")
    async def schedule_update(self, ctx: Ctx):
        b = dict(ctx.body)
        b["channel_id"] = self._check_channel(ctx.guild_id, b.get("channel_id"))
        return await self.c.automation.save_schedule(ctx.guild_id, b, ctx.user_id, schedule_id=as_int(ctx.params["sid"]))

    @route("DELETE", "/automation/schedules/{sid}", "automation.manage")
    async def schedule_delete(self, ctx: Ctx):
        if not await self.c.automation.delete_schedule(ctx.guild_id, as_int(ctx.params["sid"]), ctx.user_id):
            raise ApiError(404, "Scheduled message not found.")
        return {"ok": True}

    def _automation_body(self, ctx: Ctx) -> dict:
        b = dict(ctx.body)
        acts = []
        for a in b.get("actions") or []:
            a = dict(a or {})
            if "channel_id" in a:
                a["channel_id"] = self._check_channel(ctx.guild_id, a["channel_id"])
            if "role_id" in a:
                a["role_id"] = self._check_role(ctx.guild_id, a["role_id"])
            acts.append(a)
        b["actions"] = acts
        return b

    @route("PUT", "/automation/{aid}", "automation.manage")
    async def automation_update(self, ctx: Ctx):
        return await self.c.automation.save(ctx.guild_id, self._automation_body(ctx), ctx.user_id, automation_id=as_int(ctx.params["aid"]))

    @route("POST", "/automation/{aid}/toggle", "automation.manage")
    async def automation_toggle(self, ctx: Ctx):
        return await self.c.automation.set_enabled(ctx.guild_id, as_int(ctx.params["aid"]), bool(ctx.b("enabled")), ctx.user_id)

    @route("DELETE", "/automation/{aid}", "automation.manage")
    async def automation_delete(self, ctx: Ctx):
        if not await self.c.automation.delete(ctx.guild_id, as_int(ctx.params["aid"]), ctx.user_id):
            raise ApiError(404, "Automation not found.")
        return {"ok": True}

    # ================================================================== logging
    async def _log_query(self, ctx: Ctx, limit_all: bool = False):
        where, params = ["guild_id = ?"], [ctx.guild_id]
        if ctx.q("category"):
            where.append("category = ?")
            params.append(ctx.q("category"))
        if q := ctx.q("q").strip():
            where.append("(title LIKE ? OR detail LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if ctx.q("actor_id"):
            where.append("actor_id = ?")
            params.append(_opt_int(ctx.q("actor_id")))
        if ctx.q("since"):
            where.append("at >= ?")
            params.append(ctx.q("since"))
        if ctx.q("until"):
            where.append("at < ?")
            params.append(ctx.q("until"))
        return " FROM dispatch_log WHERE " + " AND ".join(where), params

    @route("GET", "/logs", "logs.view")
    async def logs(self, ctx: Ctx):
        from core.log_categories import LOG_CATEGORIES
        base, params = await self._log_query(ctx)
        pg = await paginate_sql(self.bot.db, "SELECT *" + base + " ORDER BY id DESC", "SELECT COUNT(*)" + base, params, ctx.qi("page"), 50)
        for it in pg.items:
            it["detail"] = loads(it.get("detail"), {})
            it["actor_id"] = _sid(it.get("actor_id"))
        out = pg.as_dict()
        out["categories"] = {k: {"label": v[0], "icon": v[1]} for k, v in LOG_CATEGORIES.items()}
        return out

    @route("GET", "/logs/export.csv", "logs.export")
    async def logs_export(self, ctx: Ctx):
        base, params = await self._log_query(ctx)
        rows = await self.bot.db.fetchall("SELECT at, category, title, detail, actor_id, incident_id" + base + " ORDER BY id DESC LIMIT 10000", params)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time (UTC)", "category", "title", "details", "actor id", "incident id"])
        for r in rows:
            d = loads(r["detail"], {}) or {}
            text = " | ".join([d.get("description") or ""] + [f"{f[0]}: {f[1]}" for f in d.get("fields") or [] if len(f) > 1]).strip(" |")
            w.writerow([r["at"], r["category"], r["title"], text, r["actor_id"] or "", r["incident_id"] or ""])
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "dashboard.export", "logs", {"rows": len(rows)})
        return CsvResponse(buf.getvalue(), "pulse-logs.csv")

    @route("GET", "/logs/audit", "logs.view")
    async def audit(self, ctx: Ctx):
        where, params = "WHERE guild_id = ?", [ctx.guild_id]
        if q := ctx.q("q").strip():
            where += " AND (action LIKE ? OR target LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]
        pg = await paginate_sql(self.bot.db, f"SELECT * FROM audit_log {where} ORDER BY id DESC", f"SELECT COUNT(*) FROM audit_log {where}",
                                params, ctx.qi("page"), 50)
        for it in pg.items:
            it["detail"] = loads(it.get("detail"), None)
            it["actor_id"] = _sid(it.get("actor_id"))
        return pg.as_dict()

    @route("GET", "/logs/settings", "admin.config")
    async def log_settings(self, ctx: Ctx):
        from core.log_categories import LOG_CATEGORIES
        s = await self.bot.logbook.settings(ctx.guild_id)
        main = await self.bot.db.get_config(ctx.guild_id, "log_channel_id")
        return {"categories": {k: {"label": v[0], "icon": v[1], "enabled": bool(s["categories"].get(k, True)),
                                   "channel_id": _sid(s["channels"].get(k))} for k, v in LOG_CATEGORIES.items()},
                "main_channel_id": _sid(main)}

    @route("PUT", "/logs/settings", "admin.config")
    async def log_settings_save(self, ctx: Ctx):
        from core.log_categories import LOG_CATEGORIES
        lb = self.bot.logbook
        if "main_channel_id" in ctx.body:
            cid = self._check_channel(ctx.guild_id, ctx.b("main_channel_id"))
            if cid:
                await self.bot.db.set_config(ctx.guild_id, "log_channel_id", cid)
            else:
                await self.bot.db.delete_config(ctx.guild_id, "log_channel_id")
        for k, v in (ctx.b("categories") or {}).items():
            if k not in LOG_CATEGORIES:
                raise ApiError(400, f"Unknown log category {k}.")
            if "enabled" in v:
                await lb.set_category(ctx.guild_id, k, bool(v["enabled"]))
            if "channel_id" in v:
                await lb.set_category_channel(ctx.guild_id, k, self._check_channel(ctx.guild_id, v["channel_id"]))
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "logs.settings", None)
        return await self.log_settings(ctx)

    # ================================================================== bot management
    @route("GET", "/embeds", "embeds.manage")
    async def embeds(self, ctx: Ctx):
        from community.embeds import PLACEHOLDERS, PRESETS
        return {"templates": await self.c.embeds.list(ctx.guild_id), "presets": list(PRESETS), "placeholders": PLACEHOLDERS}

    @route("POST", "/embeds", "embeds.manage")
    async def embed_create(self, ctx: Ctx):
        return await self.c.embeds.save(ctx.guild_id, ctx.b("name"), ctx.b("data") or {}, ctx.user_id)

    @route("POST", "/embeds/preset", "embeds.manage")
    async def embed_preset(self, ctx: Ctx):
        return await self.c.embeds.create_from_preset(ctx.guild_id, str(ctx.b("preset") or ""), ctx.user_id)

    @route("POST", "/embeds/send", "announcement.send")
    async def embed_send(self, ctx: Ctx):
        cid = self._check_channel(ctx.guild_id, ctx.b("channel_id"))
        if not cid:
            raise ApiError(400, "Choose a channel.")
        mid = await self.c.embeds.send(ctx.guild_id, cid, actor_id=ctx.user_id, template_id=_opt_int(ctx.b("template_id")),
                                       data=ctx.b("data") or None, content=ctx.b("content") or None)
        return {"message_id": str(mid)}

    @route("POST", "/embeds/preview", "embeds.manage")
    async def embed_preview(self, ctx: Ctx):
        from community.embeds import render, validate_embed
        return render(validate_embed(ctx.b("data") or {}), await self.c.embeds.values(ctx.guild_id, ctx.user_id))

    @route("PUT", "/embeds/{tid}", "embeds.manage")
    async def embed_update(self, ctx: Ctx):
        return await self.c.embeds.save(ctx.guild_id, ctx.b("name"), ctx.b("data") or {}, ctx.user_id, template_id=as_int(ctx.params["tid"]))

    @route("DELETE", "/embeds/{tid}", "embeds.manage")
    async def embed_delete(self, ctx: Ctx):
        if not await self.c.embeds.delete(ctx.guild_id, as_int(ctx.params["tid"]), ctx.user_id):
            raise ApiError(404, "Template not found.")
        return {"ok": True}

    @route("GET", "/welcome", "embeds.manage")
    async def welcome(self, ctx: Ctx):
        s = await self.c.members.welcome_settings(ctx.guild_id)
        return {**{k: (_sid(v) if k.endswith("_id") else v) for k, v in s.items()}, "auto_role_ids": [str(r) for r in s.get("auto_role_ids") or []]}

    @route("PUT", "/welcome", "embeds.manage")
    async def welcome_save(self, ctx: Ctx):
        from community.members import WELCOME_DEFAULTS
        b, changes = ctx.body, {}
        for k in ("enabled", "dm", "leave_enabled"):
            if k in b:
                changes[k] = bool(b[k])
        for k in ("channel_id", "leave_channel_id"):
            if k in b:
                changes[k] = self._check_channel(ctx.guild_id, b[k])
        if "template_id" in b:
            tid = _opt_int(b["template_id"])
            if tid and not await self.c.embeds.get(ctx.guild_id, tid):
                raise ApiError(400, "Template not found.")
            changes["template_id"] = tid
        for k in ("message", "leave_message"):
            if k in b:
                changes[k] = clean_text(b[k], 1500)
        if "auto_role_ids" in b:
            changes["auto_role_ids"] = [self._check_role(ctx.guild_id, r) for r in (b["auto_role_ids"] or [])][:5]
        await self.c.update_settings(ctx.guild_id, "welcome_settings", WELCOME_DEFAULTS, changes, ctx.user_id)
        return await self.welcome(ctx)

    @route("GET", "/commands", "dashboard.view")
    async def commands(self, ctx: Ctx):
        import datetime as dt
        since = (utcnow() - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        usage = {r["command"]: r["n"] for r in await self.bot.db.fetchall(
            "SELECT command, COUNT(*) n FROM command_usage WHERE guild_id = ? AND at >= ? GROUP BY command", (ctx.guild_id, since))}
        out = []
        tree = getattr(self.bot, "tree", None)
        for cmd in (tree.get_commands() if tree else []):
            subs = getattr(cmd, "commands", None)
            if subs:
                for s in subs:
                    out.append({"name": s.qualified_name, "description": s.description, "uses": usage.get(s.qualified_name, 0)})
            else:
                out.append({"name": cmd.qualified_name, "description": getattr(cmd, "description", ""), "uses": usage.get(cmd.qualified_name, 0)})
        return {"commands": sorted(out, key=lambda c: c["name"]), "total_uses": sum(usage.values())}

    @route("GET", "/bot/appearance", "admin.config")
    async def appearance(self, ctx: Ctx):
        from core.branding import DEFAULT_EMBED_SETTINGS
        s = {**DEFAULT_EMBED_SETTINGS, **(await self.bot.db.get_config(ctx.guild_id, "embed_settings", {}) or {})}
        return {**s, "accent_hex": f"#{int(s['accent']):06X}"}

    @route("PUT", "/bot/appearance", "admin.config")
    async def appearance_save(self, ctx: Ctx):
        from core.branding import DEFAULT_EMBED_SETTINGS
        from community.embeds import parse_colour
        s = {**DEFAULT_EMBED_SETTINGS, **(await self.bot.db.get_config(ctx.guild_id, "embed_settings", {}) or {})}
        if "accent" in ctx.body:
            s["accent"] = parse_colour(ctx.b("accent")) or DEFAULT_EMBED_SETTINGS["accent"]
        for k in ("compact", "show_thumbnails"):
            if k in ctx.body:
                s[k] = bool(ctx.b(k))
        if "footer_note" in ctx.body:
            s["footer_note"] = clean_text(ctx.b("footer_note"), 60)
        await self.bot.db.set_config(ctx.guild_id, "embed_settings", s)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "settings.embed", None, s)
        return await self.appearance(ctx)

    # ================================================================== AI
    @route("GET", "/ai", "admin.ai")
    async def ai(self, ctx: Ctx):
        providers = await self.bot.ai.guild_providers(ctx.guild_id)
        for p in providers:
            p.pop("cooldown_until", None)
        return {"providers": providers, "settings": await self.bot.ai.ai_settings(ctx.guild_id),
                "active": self.bot.ai.active_provider(ctx.guild_id), "usage_today": await self.bot.ai.usage_summary(ctx.guild_id)}

    @route("PUT", "/ai/settings", "admin.ai")
    async def ai_settings(self, ctx: Ctx):
        changes = {}
        if ctx.b("mode") in {"auto", "manual"}:
            changes["mode"] = ctx.b("mode")
        if "failover" in ctx.body:
            changes["failover"] = bool(ctx.b("failover"))
        if "preferred" in ctx.body:
            pref = ctx.b("preferred") or None
            if pref and pref not in self.bot.ai.providers:
                raise ApiError(400, "Unknown provider.")
            changes["preferred"] = pref
            if pref:
                await self.bot.ai.set_order(ctx.guild_id, pref)
        s = await self.bot.ai.update_ai_settings(ctx.guild_id, **changes)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "ai.settings", None, changes)
        return s

    def _provider(self, ctx: Ctx) -> str:
        name = ctx.params["name"]
        if name not in self.bot.ai.providers:
            raise ApiError(404, "Unknown provider.")
        return name

    @route("POST", "/ai/providers/{name}", "admin.ai")
    async def ai_provider(self, ctx: Ctx):
        name = self._provider(ctx)
        if "enabled" in ctx.body:
            await self.bot.ai.set_enabled(ctx.guild_id, name, bool(ctx.b("enabled")))
        if "model" in ctx.body:
            await self.bot.ai.set_model(ctx.guild_id, name, clean_text(ctx.b("model"), 120) or None)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "ai.provider", name, {k: ctx.body[k] for k in ("enabled", "model") if k in ctx.body})
        return {"ok": True}

    @route("POST", "/ai/providers/{name}/key", "admin.ai")
    async def ai_key(self, ctx: Ctx):
        """Write-only: the key is encrypted and never sent back to any browser."""
        from ai.providers.catalog import SPEC, looks_like_key
        name = self._provider(ctx)
        sp = SPEC.get(name)
        if sp is None or not sp.needs_key:
            raise ApiError(400, "This provider is configured in the bot's .env file, not per server.")
        key = str(ctx.b("key") or "").strip()
        if len(key) < 8 or len(key) > 400:
            raise ApiError(400, "That doesn't look like an API key.")
        await self.bot.secrets.set(ctx.guild_id, f"ai_key:{name}", key, ctx.user_id)
        if sp.needs_account_id and ctx.b("account_id"):
            await self.bot.secrets.set(ctx.guild_id, f"ai_account:{name}", str(ctx.b("account_id")).strip()[:64], ctx.user_id)
        await self.bot.ai.clear_cooldown(ctx.guild_id, name)
        await self.bot.ai.set_enabled(ctx.guild_id, name, True)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "ai.key_set", name, {"key": "***"})
        ok, msg = await self.bot.ai.test_provider(ctx.guild_id, name)
        return {"saved": True, "test_ok": ok, "message": msg, "warning": looks_like_key(name, key)}

    @route("DELETE", "/ai/providers/{name}/key", "admin.ai")
    async def ai_key_delete(self, ctx: Ctx):
        name = self._provider(ctx)
        await self.bot.secrets.delete(ctx.guild_id, f"ai_key:{name}")
        await self.bot.secrets.delete(ctx.guild_id, f"ai_account:{name}")
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "ai.key_removed", name)
        return {"ok": True}

    @route("POST", "/ai/providers/{name}/test", "admin.ai")
    async def ai_test(self, ctx: Ctx):
        ok, msg = await self.bot.ai.test_provider(ctx.guild_id, self._provider(ctx))
        return {"ok": ok, "message": msg}

    @route("GET", "/ai/usage", "admin.ai")
    async def ai_usage(self, ctx: Ctx):
        data = await self.c.analytics.series(ctx.guild_id, ["ai_requests"], ctx.q("range", "30d"))
        rows = row_dicts(await self.bot.db.fetchall(
            "SELECT provider, model, purpose, ok, latency_ms, error, at FROM ai_usage WHERE guild_id = ? ORDER BY id DESC LIMIT 60", (ctx.guild_id,)))
        by = row_dicts(await self.bot.db.fetchall(
            "SELECT provider, COUNT(*) n, SUM(ok) ok, ROUND(AVG(CASE WHEN ok THEN latency_ms END)) avg_ms FROM ai_usage WHERE guild_id = ? "
            "AND at >= ? GROUP BY provider ORDER BY n DESC", (ctx.guild_id, data["range"]["start"])))
        return {"series": data, "recent": rows, "by_provider": by}

    @route("GET", "/ai/personality", "admin.config")
    async def personality(self, ctx: Ctx):
        from dispatch.phrases import STYLES
        return {"response_style": await self.core.setting(ctx.guild_id, "response_style"), "agency_name": await self.core.setting(ctx.guild_id, "agency_name"),
                "follow_up_questions": await self.core.setting(ctx.guild_id, "follow_up_questions"),
                "ai_announcement_wording": await self.core.setting(ctx.guild_id, "ai_announcement_wording"), "styles": STYLES}

    @route("PUT", "/ai/personality", "admin.config")
    async def personality_save(self, ctx: Ctx):
        ctx.body = {"settings": {k: ctx.body[k] for k in ("response_style", "agency_name", "follow_up_questions", "ai_announcement_wording")
                                 if k in ctx.body}}
        await self.dispatch_settings_save(ctx)
        return await self.personality(ctx)

    # ================================================================== settings
    CHANNEL_KEYS = {"log_channel_id": "Main log channel", "admin_channel_id": "Administration channel",
                    "department_channel_id": "Department / supervisor alerts", "dispatch_channel_id": "Plain-language #dispatch channel"}

    @route("GET", "/settings", "admin.config")
    async def settings(self, ctx: Ctx):
        from erlc.authentication import SERVER_KEY_NAME
        out = {"channels": {k: {"label": v, "value": _sid(await self.bot.db.get_config(ctx.guild_id, k))} for k, v in self.CHANNEL_KEYS.items()},
               "erlc": {"key_set": await self.bot.secrets.exists(ctx.guild_id, SERVER_KEY_NAME),
                        "monitoring": await self.bot.db.get_config(ctx.guild_id, "erlc_monitoring_enabled", True)}}
        return out

    @route("PUT", "/settings/channels", "admin.config")
    async def settings_channels(self, ctx: Ctx):
        for k, v in ctx.body.items():
            if k not in self.CHANNEL_KEYS:
                raise ApiError(400, f"Unknown channel setting {k}.")
            cid = self._check_channel(ctx.guild_id, v)
            if cid:
                await self.bot.db.set_config(ctx.guild_id, k, cid)
            else:
                await self.bot.db.delete_config(ctx.guild_id, k)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "settings.channels", None, {k: str(v) for k, v in ctx.body.items()})
        return await self.settings(ctx)

    @route("POST", "/settings/erlc-key", "admin.erlc")
    async def settings_erlc_key(self, ctx: Ctx):
        """Write-only. The key is tested against the ER:LC API before it is saved (encrypted)."""
        from erlc.authentication import SERVER_KEY_NAME, looks_like_server_key
        key = str(ctx.b("key") or "").strip()
        if not looks_like_server_key(key):
            raise ApiError(400, "That doesn't look like an ER:LC server key (in-game: Settings → API).")
        ok, msg, _ = await self.bot.sync.test_connection(ctx.guild_id, key)
        if not ok:
            raise ApiError(400, f"Key not saved — the ER:LC API rejected it: {msg}")
        await self.bot.secrets.set(ctx.guild_id, SERVER_KEY_NAME, key, ctx.user_id)
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "erlc.key_set")
        self.bot.sync.start_guild(ctx.guild_id)
        await self.bot.sync.reset_client(ctx.guild_id)
        return {"ok": True, "message": msg}

    @route("PUT", "/settings/erlc-monitoring", "admin.erlc")
    async def settings_erlc_monitoring(self, ctx: Ctx):
        await self.bot.db.set_config(ctx.guild_id, "erlc_monitoring_enabled", bool(ctx.b("enabled")))
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "erlc.monitoring", None, {"enabled": bool(ctx.b("enabled"))})
        self.bot.sync.trigger(ctx.guild_id)
        return {"ok": True}

    @route("GET", "/settings/permissions", "admin.permissions")
    async def permissions(self, ctx: Ctx):
        from security.permissions import DEFAULT_PERMISSIONS, GROUP_LABEL, GROUPS, PERMISSION_DESCRIPTIONS
        pm = self.bot.permissions
        role_map = await pm.role_map(ctx.guild_id)
        ov = await pm.overrides(ctx.guild_id)
        return {"groups": [{"key": g, "label": GROUP_LABEL[g], "roles": [str(r) for r in role_map.get(g, [])]} for g in GROUPS],
                "permissions": [{"key": p, "description": PERMISSION_DESCRIPTIONS.get(p, p), "default": sorted(d),
                                 "groups": sorted(ov[p]) if p in ov else sorted(d), "overridden": p in ov}
                                for p, d in DEFAULT_PERMISSIONS.items()]}

    @route("PUT", "/settings/permissions/groups/{group}", "admin.permissions")
    async def permissions_group(self, ctx: Ctx):
        group = ctx.params["group"]
        if group in {"owner", "citizen"}:
            raise ApiError(400, "That group is automatic and has no roles.")
        roles = [self._check_role(ctx.guild_id, r) for r in ctx.b("roles") or []]
        await self.bot.permissions.set_group_roles(ctx.guild_id, group, [r for r in roles if r])
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "permissions.map", group, {"roles": [str(r) for r in roles]})
        return await self.permissions(ctx)

    @route("PUT", "/settings/permissions/overrides/{perm}", "admin.permissions")
    async def permissions_override(self, ctx: Ctx):
        from security.permissions import DEFAULT_PERMISSIONS
        perm = ctx.params["perm"]
        if perm not in DEFAULT_PERMISSIONS:
            raise ApiError(404, "Unknown permission.")
        groups = ctx.b("groups")
        await self.bot.permissions.set_override(ctx.guild_id, perm, None if groups is None else list(groups))
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "permissions.override", perm, {"groups": groups})
        return await self.permissions(ctx)

    @route("GET", "/settings/notifications", "admin.config")
    async def notification_settings(self, ctx: Ctx):
        from community.notifications import KINDS
        s = await self.c.notifications.settings(ctx.guild_id)
        return {"kinds": KINDS, "disabled": s["disabled"], "channels": {k: _sid(v) for k, v in (s["channels"] or {}).items()}}

    @route("PUT", "/settings/notifications", "admin.config")
    async def notification_settings_save(self, ctx: Ctx):
        from community.notifications import DEFAULTS, KINDS
        changes = {}
        if "disabled" in ctx.body:
            changes["disabled"] = [k for k in ctx.b("disabled") or [] if k in KINDS]
        if "channels" in ctx.body:
            chans = {}
            for k, v in (ctx.b("channels") or {}).items():
                if k in KINDS and (cid := self._check_channel(ctx.guild_id, v)):
                    chans[k] = cid
            changes["channels"] = chans
        await self.c.update_settings(ctx.guild_id, "notification_settings", DEFAULTS, changes, ctx.user_id)
        return await self.notification_settings(ctx)

    @route("GET", "/settings/integrations", "dashboard.view")
    async def integrations(self, ctx: Ctx):
        sync = await self.bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (ctx.guild_id,))
        providers = await self.bot.ai.guild_providers(ctx.guild_id)
        s = self.bot.settings
        return {
            "erlc": {"configured": sync is not None, "connected": bool(sync and sync["connected"]),
                     "server_name": sync["server_name"] if sync else None, "players": sync["current_players"] if sync else None,
                     "max_players": sync["max_players"] if sync else None, "last_success_at": sync["last_success_at"] if sync else None,
                     "last_error": sync["last_error"] if sync else None, "rate_limit_remaining": sync["rate_limit_remaining"] if sync else None,
                     "webhook": bool(s.webhook_enabled), "webhook_last_at": sync["webhook_last_at"] if sync else None,
                     "capabilities": [
                         {"name": "In-game 911 calls", "available": True, "how": "Polled from the official ER:LC API (GET /v2/server, EmergencyCalls)"},
                         {"name": "Server name and player count", "available": True, "how": "Official ER:LC API"},
                         {"name": "Event webhooks", "available": bool(s.webhook_enabled), "how": "Optional; needs a public HTTPS address"},
                         {"name": "Running in-game commands", "available": False,
                          "how": "Supported by the API but not exposed by Pulse Dispatch for safety. Use the in-game console."},
                         {"name": "Staff or player bans in ER:LC", "available": False,
                          "how": "Not part of what Pulse Dispatch manages. Handle bans in game; record them as a case here."}]},
            "roblox": {"profile_code": True, "oauth_configured": self.c.verification.oauth_available(),
                       "oauth_ready": self.c.verification.oauth_available() and bool(s.dashboard_public_url)},
            "ai": {"providers_ready": sum(1 for p in providers if p["configured"] and p["enabled"]), "providers_total": len(providers)},
            "dashboard": {"public_url": s.dashboard_public_url or None, "local_url": f"http://localhost:{s.dashboard_port}"},
        }

    # ================================================================== notifications & assistant
    @route("GET", "/notifications", "dashboard.view")
    async def notifications(self, ctx: Ctx):
        items = await self.c.notifications.list_for(ctx.guild_id, ctx.user_id, ctx.perms, limit=40, unread_only=ctx.q("unread") == "1")
        await self.humanize(ctx.guild_id, items)
        return {"items": items, "unread": sum(1 for i in items if not i["read"])}

    @route("POST", "/notifications/read", "dashboard.view")
    async def notifications_read(self, ctx: Ctx):
        ids = ctx.b("ids")
        n = await self.c.notifications.mark_read(ctx.guild_id, ctx.user_id, None if ids == "all" else [int(i) for i in ids or []])
        return {"marked": n}

    @route("POST", "/assistant", "assistant.use")
    async def assistant(self, ctx: Ctx):
        q = clean_text(ctx.b("question"), 500, required=True, field="Question")
        r = await self.c.assistant.ask(ctx.guild_id, ctx.user_id, ctx.perms, q)
        return r.as_dict()

    @route("POST", "/assistant/confirm", "assistant.use")
    async def assistant_confirm(self, ctx: Ctx):
        r = await self.c.assistant.confirm(ctx.guild_id, ctx.user_id, ctx.perms, str(ctx.b("action") or ""), ctx.b("args") or {})
        return r.as_dict()


class CsvResponse:
    def __init__(self, text: str, filename: str):
        self.text = text
        self.filename = filename


__all__ = ["Api", "ApiError", "Ctx", "CsvResponse", "NAV", "ROUTES"]
