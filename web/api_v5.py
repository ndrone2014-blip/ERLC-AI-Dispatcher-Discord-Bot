"""v5.0 Platform Overhaul dashboard routes: ER:LC live, in-game moderation, BOLOs, staff overview,
activity feed and global search.

Imported by `web/api.py` *before* the v4 routes, so specific paths such as /moderation/records win over
v4 patterns such as /moderation/{n}. Every route declares its permission; handlers that need more than one
check them explicitly. All data is scoped to the guild in the URL.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from community.common import as_int, clean_text, iso_now, paginate_sql, row_dicts, utcnow
from core.activity import FEED_GROUPS, visible_categories

from .api import ApiError, Ctx, _opt_int, _sid, route

BOLO_NOT_FOUND = "BOLO not found."
COMMAND_RE = re.compile(r"^:[A-Za-z]{1,20}(\s.*)?$")


class PlatformApi:
    """Mixin for `web.api.Api` (uses self.bot, self.c, self.core)."""

    # ------------------------------------------------------------------ helpers
    @property
    def live(self):
        return getattr(self.bot, "erlc_live", None)

    def _need_any(self, ctx: Ctx, *perms: str) -> None:
        if not any(p in ctx.perms for p in perms):
            raise ApiError(403, "You don't have permission to do that.")

    async def _names(self, gid: int, ids) -> dict[str, dict]:
        ids = {int(i) for i in ids if i}
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self.bot.db.fetchall(f"SELECT user_id, username, display_name, avatar_url FROM members WHERE guild_id = ? AND user_id IN ({marks})",
                                          (gid, *ids))
        return {str(r["user_id"]): {"name": r["display_name"] or r["username"], "avatar": r["avatar_url"]} for r in rows}

    MENTION = re.compile(r"<@!?(\d{2,22})>")

    async def humanize(self, gid: int, items: list[dict], fields=("title", "body")) -> list[dict]:
        """Discord-style mentions (<@123>) become readable names for the dashboard."""
        ids = {m for it in items for f in fields for m in self.MENTION.findall(str(it.get(f) or ""))}
        if not ids:
            return items
        names = await self._names(gid, ids)
        def sub(m):
            n = names.get(m.group(1))
            return f"@{n['name']}" if n and n.get("name") else "@member"
        for it in items:
            for f in fields:
                if it.get(f):
                    it[f] = self.MENTION.sub(sub, str(it[f]))
        return items

    # ================================================================== activity feed
    @route("GET", "/activity", "dashboard.view")
    async def activity_feed(self, ctx: Ctx):
        where, params = ["guild_id = ?"], [ctx.guild_id]
        allowed = set(visible_categories(ctx.perms))
        group = ctx.q("group")
        cats = [c for c in (FEED_GROUPS[group][1] if group in FEED_GROUPS else allowed) if c in allowed] or ["-"]
        where.append(f"category IN ({','.join('?' * len(cats))})")
        params += list(cats)
        if q := ctx.q("q").strip():
            where.append("(title LIKE ? OR body LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if after := _opt_int(ctx.q("after")):
            where.append("id > ?")
            params.append(after)
        base = " FROM activity_events WHERE " + " AND ".join(where)
        pg = await paginate_sql(self.bot.db, "SELECT *" + base + " ORDER BY id DESC", "SELECT COUNT(*)" + base, params, ctx.qi("page"),
                                min(100, max(10, ctx.qi("per_page", 30))))
        names = await self._names(ctx.guild_id, [i["actor_id"] for i in pg.items])
        for i in pg.items:
            i["actor_id"] = _sid(i["actor_id"])
            i["actor"] = names.get(i["actor_id"] or "")
        await self.humanize(ctx.guild_id, pg.items)
        out = pg.as_dict()
        out["groups"] = [{"key": k, "label": v[0]} for k, v in FEED_GROUPS.items() if any(c in allowed for c in v[1])]
        return out

    # ================================================================== global search
    @route("GET", "/search", "dashboard.view")
    async def search(self, ctx: Ctx):
        q = clean_text(ctx.q("q"), 60)
        if len(q) < 2:
            return {"q": q, "results": []}
        gid, db, like, num = ctx.guild_id, self.bot.db, f"%{q}%", q.lstrip("#")
        res: list[dict] = []

        def add(kind, title, sub, link, icon):
            res.append({"kind": kind, "title": title, "subtitle": sub, "link": link, "icon": icon})
        if "members.view" in ctx.perms:
            for r in await db.fetchall("SELECT user_id, username, display_name FROM members WHERE guild_id = ? AND left_at IS NULL AND "
                                       "(username LIKE ? OR display_name LIKE ? OR CAST(user_id AS TEXT) = ?) LIMIT 5", (gid, like, like, q)):
                add("Member", r["display_name"] or r["username"], f"@{r['username']}", f"members/{r['user_id']}", "user")
        if "staff.view" in ctx.perms:
            for r in await db.fetchall("SELECT s.user_id, s.rank, m.username, m.display_name FROM staff s LEFT JOIN members m ON m.guild_id = s.guild_id "
                                       "AND m.user_id = s.user_id WHERE s.guild_id = ? AND s.status != 'removed' AND (m.username LIKE ? OR "
                                       "m.display_name LIKE ? OR s.rank LIKE ?) LIMIT 5", (gid, like, like, like)):
                add("Staff", r["display_name"] or r["username"] or r["user_id"], r["rank"], f"staff/{r['user_id']}", "badge")
        if "erlc.view" in ctx.perms:
            for r in await db.fetchall("SELECT roblox_id, username, online FROM erlc_players WHERE guild_id = ? AND (username LIKE ? OR "
                                       "CAST(roblox_id AS TEXT) = ?) ORDER BY online DESC, last_seen DESC LIMIT 5", (gid, like, q)):
                add("Player", r["username"], "In-game now" if r["online"] else f"Roblox {r['roblox_id']}", f"moderation/player/{r['roblox_id']}", "gamepad")
            for r in await db.fetchall("SELECT number, action, roblox_username, reason FROM erlc_mod_records WHERE guild_id = ? AND "
                                       "(CAST(number AS TEXT) = ? OR roblox_username LIKE ?) ORDER BY id DESC LIMIT 5", (gid, num, like)):
                add("In-game record", f"#{r['number']} {r['action']} · {r['roblox_username']}", (r["reason"] or "")[:80],
                    f"moderation/records/{r['number']}", "gavel")
        if "incident.view" in ctx.perms:
            for r in await db.fetchall("SELECT number, nature, location, status FROM incidents WHERE guild_id = ? AND (CAST(number AS TEXT) = ? "
                                       "OR nature LIKE ? OR location LIKE ?) ORDER BY id DESC LIMIT 5", (gid, num, like, like)):
                add("Incident", f"#{r['number']} {r['nature'] or 'Incident'}", f"{r['status']} · {r['location'] or 'No location'}",
                    f"dispatch/incident/{r['number']}", "siren")
        if "bolo.view" in ctx.perms:
            for r in await db.fetchall("SELECT number, subject, status, priority FROM bolos WHERE guild_id = ? AND (CAST(number AS TEXT) = ? OR "
                                       "subject LIKE ? OR roblox_username LIKE ? OR plate LIKE ?) ORDER BY id DESC LIMIT 5",
                                       (gid, num, like, like, like)):
                add("BOLO", f"#{r['number']} {r['subject']}", f"{r['priority']} · {'active' if r['status'] == 'active' else 'closed'}",
                    f"bolos/{r['number']}", "eye")
        if "mod.view" in ctx.perms:
            for r in await db.fetchall("SELECT number, action, target_name, target_id FROM mod_cases WHERE guild_id = ? AND (CAST(number AS TEXT) = ? "
                                       "OR target_name LIKE ?) ORDER BY id DESC LIMIT 5", (gid, num, like)):
                add("Discord case", f"#{r['number']} {r['action']} · {r['target_name'] or r['target_id']}", "", f"moderation/discord/{r['number']}", "shield")
        if "application.review" in ctx.perms:
            for r in await db.fetchall("SELECT a.number, a.form_name, a.status, m.display_name, m.username FROM applications a LEFT JOIN members m "
                                       "ON m.guild_id = a.guild_id AND m.user_id = a.user_id WHERE a.guild_id = ? AND (CAST(a.number AS TEXT) = ? OR "
                                       "a.form_name LIKE ? OR m.username LIKE ? OR m.display_name LIKE ?) ORDER BY a.id DESC LIMIT 5",
                                       (gid, num, like, like, like)):
                add("Application", f"#{r['number']} {r['form_name']}", f"{r['display_name'] or r['username'] or ''} · {r['status']}",
                    f"applications/{r['number']}", "file")
        return {"q": q, "results": res}

    # ================================================================== ER:LC live
    async def _sync_state(self, gid: int) -> dict:
        row = await self.bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (gid,))
        gs = self.bot.sync.state(gid) if getattr(self.bot, "sync", None) else None
        status = gs.status if gs else ("connected" if row and row["connected"] else "unknown")
        return {"status": status, "detail": gs.detail if gs else None,
                "connected": status == "connected" if gs else bool(row and row["connected"]),
                "last_success_at": row["last_success_at"] if row else None, "last_error": row["last_error"] if row else None,
                "configured": bool(gs and gs.status not in {"not_configured"}) if gs else bool(row)}

    @route("GET", "/erlc/live", "erlc.view")
    async def erlc_live(self, ctx: Ctx):
        conn = await self._sync_state(ctx.guild_id)
        snap = self.live.snapshot(ctx.guild_id) if self.live else None
        recent = row_dicts(await self.bot.db.fetchall(
            "SELECT roblox_id, username, kind, at, team FROM erlc_player_events WHERE guild_id = ? ORDER BY id DESC LIMIT 25", (ctx.guild_id,)))
        for r in recent:
            r["roblox_id"] = str(r["roblox_id"])
        bolo_rows = await self.bot.db.fetchall("SELECT number, roblox_id, LOWER(roblox_username) AS u, priority FROM bolos WHERE guild_id = ? "
                                               "AND status = 'active' AND (roblox_id IS NOT NULL OR roblox_username IS NOT NULL)", (ctx.guild_id,))
        flagged = {}
        for b in bolo_rows:
            for key in (str(b["roblox_id"]) if b["roblox_id"] else None, b["u"]):
                if key:
                    flagged[key] = {"number": b["number"], "priority": b["priority"]}
        if snap:
            for p in snap["players"]:
                p["bolo"] = flagged.get(p["roblox_id"] or "") or flagged.get(p["name"].lower())
        today = utcnow().strftime("%Y-%m-%d")
        stats = {"joins_today": await self.bot.db.fetchval("SELECT COUNT(*) FROM erlc_player_events WHERE guild_id = ? AND kind = 'join' AND at >= ?",
                                                           (ctx.guild_id, today), 0),
                 "unique_today": await self.bot.db.fetchval("SELECT COUNT(DISTINCT roblox_id) FROM erlc_player_events WHERE guild_id = ? AND "
                                                            "kind = 'join' AND at >= ?", (ctx.guild_id, today), 0),
                 "known_players": await self.bot.db.fetchval("SELECT COUNT(*) FROM erlc_players WHERE guild_id = ?", (ctx.guild_id,), 0)}
        return {"connection": conn, "live": snap, "recent": recent, "stats": stats, "can_command": "erlc.command" in ctx.perms,
                "extras_enabled": bool(await self.bot.db.get_config(ctx.guild_id, "erlc_live_extras", True))}

    @route("GET", "/erlc/logs/{section}", "erlc.view")
    async def erlc_logs(self, ctx: Ctx):
        section = ctx.params["section"]
        if section not in {"join_logs", "kill_logs", "command_logs", "mod_calls", "queue", "staff"}:
            raise ApiError(404, "Unknown log section.")
        data = self.live.section(ctx.guild_id, section) if self.live else None
        return {"section": section, "available": data is not None, "items": data or []}

    @route("GET", "/erlc/players", "erlc.view")
    async def erlc_players(self, ctx: Ctx):
        online = {"online": True, "offline": False}.get(ctx.q("filter"))
        return await self.c.erlc_moderation.search_players(ctx.guild_id, ctx.q("q"), online=online, page=ctx.qi("page"))

    @route("GET", "/erlc/events", "erlc.view")
    async def erlc_events(self, ctx: Ctx):
        where, params = ["guild_id = ?"], [ctx.guild_id]
        if ctx.q("kind") in {"join", "leave"}:
            where.append("kind = ?")
            params.append(ctx.q("kind"))
        if q := ctx.q("q").strip():
            where.append("(username LIKE ? OR CAST(roblox_id AS TEXT) = ?)")
            params += [f"%{q}%", q]
        base = " FROM erlc_player_events WHERE " + " AND ".join(where)
        pg = await paginate_sql(self.bot.db, "SELECT *" + base + " ORDER BY id DESC", "SELECT COUNT(*)" + base, params, ctx.qi("page"), 50)
        for i in pg.items:
            i["roblox_id"] = str(i["roblox_id"])
        return pg.as_dict()

    @route("POST", "/erlc/command", "erlc.command")
    async def erlc_command(self, ctx: Ctx):
        cmd = re.sub(r"[\r\n\t]+", " ", str(ctx.b("command") or "")).strip()
        if not COMMAND_RE.match(cmd) or len(cmd) > 300:
            raise ApiError(400, "Commands start with ':' followed by the command name, e.g. :h Server restart in 5 minutes.")
        if getattr(self.bot, "sync", None) is None:
            raise ApiError(503, "The ER:LC integration is not running.")
        try:
            await self.bot.sync.run_command(ctx.guild_id, cmd)
        except Exception as e:
            msg = e.friendly() if hasattr(e, "friendly") else str(e)
            await self.c.log(ctx.guild_id, "erlc_commands", "In-game command failed", fields=[("Command", cmd, False), ("Error", msg, False)],
                             actor_id=ctx.user_id)
            raise ApiError(400, f"ER:LC refused the command: {msg}") from e
        await self.c.audit.record(ctx.guild_id, ctx.user_id, "erlc.command", None, {"command": cmd[:120]})
        await self.c.log(ctx.guild_id, "erlc_commands", "In-game command run", fields=[("Command", cmd, False)], actor_id=ctx.user_id)
        return {"ok": True, "command": cmd}

    @route("GET", "/erlc/bans", "erlc.view")
    async def erlc_bans(self, ctx: Ctx):
        if getattr(self.bot, "sync", None) is None:
            return {"available": False, "error": "The ER:LC integration is not running.", "items": []}
        try:
            bans = await self.bot.sync.get_bans(ctx.guild_id)
        except Exception as e:
            return {"available": False, "error": e.friendly() if hasattr(e, "friendly") else str(e), "items": []}
        return {"available": True, "items": [{"roblox_id": k, "username": v} for k, v in sorted(bans.items(), key=lambda kv: kv[1].lower())]}

    @route("PUT", "/erlc/settings", "admin.erlc")
    async def erlc_settings(self, ctx: Ctx):
        if "extras" in ctx.body:
            await self.bot.db.set_config(ctx.guild_id, "erlc_live_extras", bool(ctx.b("extras")))
            await self.c.audit.record(ctx.guild_id, ctx.user_id, "settings.erlc_live_extras", None, {"enabled": bool(ctx.b("extras"))})
            if getattr(self.bot, "sync", None):
                self.bot.sync.trigger(ctx.guild_id)
        return {"extras": bool(await self.bot.db.get_config(ctx.guild_id, "erlc_live_extras", True))}

    # ================================================================== moderation center
    @route("GET", "/moderation/stats", "dashboard.view")
    async def moderation_stats(self, ctx: Ctx):
        self._need_any(ctx, "mod.view", "erlc.view")
        out = await self.c.erlc_moderation.stats(ctx.guild_id, ctx.qi("days", 30))
        names = await self._names(ctx.guild_id, [m["user_id"] for m in out["moderators"]])
        for m in out["moderators"]:
            m.setdefault("name", (names.get(m["user_id"]) or {}).get("name"))
        recent_game = [self.c.erlc_moderation._rec(r) for r in await self.bot.db.fetchall(
            "SELECT * FROM erlc_mod_records WHERE guild_id = ? ORDER BY id DESC LIMIT 8", (ctx.guild_id,))] if "erlc.view" in ctx.perms else None
        recent_discord = [self.c.moderation._case(r) for r in await self.bot.db.fetchall(
            "SELECT * FROM mod_cases WHERE guild_id = ? ORDER BY id DESC LIMIT 8", (ctx.guild_id,))] if "mod.view" in ctx.perms else None
        out["recent"] = {"in_game": recent_game, "discord": recent_discord}
        out["can"] = {k: k in ctx.perms for k in ("erlc.view", "erlc.moderate", "erlc.ban", "mod.view", "mod.warn", "mod.kick", "mod.ban",
                                                 "mod.timeout", "mod.reports_manage")}
        if "mod.view" not in ctx.perms:
            out["discord"], out["series"]["discord"] = {}, [0] * len(out["labels"])
            out["active"].update(discord_bans=None, discord_timeouts=None, open_reports=None)
        if "erlc.view" not in ctx.perms:
            out["in_game"], out["series"]["in_game"] = {}, [0] * len(out["labels"])
            out["active"]["in_game_bans"] = None
        return out

    @route("GET", "/moderation/records", "erlc.view")
    async def erlc_records(self, ctx: Ctx):
        from community.erlc_moderation import ACTIONS
        data = await self.c.erlc_moderation.list(ctx.guild_id, action=ctx.q("action"), status=ctx.q("status"), q=ctx.q("q"),
                                                 roblox_id=_opt_int(ctx.q("roblox_id")), moderator_id=_opt_int(ctx.q("moderator_id")),
                                                 since=ctx.q("since"), page=ctx.qi("page"))
        names = await self._names(ctx.guild_id, [i["moderator_id"] for i in data["items"]])
        for i in data["items"]:
            i["moderator"] = names.get(i["moderator_id"] or "")
        data["actions"] = ACTIONS
        return data

    @route("POST", "/moderation/records", "erlc.view")
    async def erlc_record_create(self, ctx: Ctx):
        from community.erlc_moderation import PERMISSION
        action = str(ctx.b("action") or "")
        if action not in PERMISSION:
            raise ApiError(400, "Unknown action.")
        ctx.need(PERMISSION[action])
        if action in {"ban", "tempban", "kick"} and ctx.b("confirm") is not True:
            raise ApiError(400, "Confirm this action first.")
        return await self.c.erlc_moderation.create(ctx.guild_id, action, str(ctx.b("player") or ""), ctx.user_id,
                                                   reason=ctx.b("reason") or "", evidence=ctx.b("evidence") or "",
                                                   duration=ctx.b("duration"), execute=bool(ctx.b("execute")))

    @route("GET", "/moderation/records/{n}", "erlc.view")
    async def erlc_record(self, ctx: Ctx):
        rec = await self.c.erlc_moderation.get(ctx.guild_id, as_int(ctx.params["n"], "Record number"))
        if not rec:
            raise ApiError(404, "Record not found.")
        names = await self._names(ctx.guild_id, [rec["moderator_id"], rec["revoked_by"]])
        rec["moderator"], rec["revoker"] = names.get(rec["moderator_id"] or ""), names.get(rec["revoked_by"] or "")
        return rec

    @route("PATCH", "/moderation/records/{n}", "erlc.moderate")
    async def erlc_record_edit(self, ctx: Ctx):
        return await self.c.erlc_moderation.edit(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, reason=ctx.b("reason"), evidence=ctx.b("evidence"))

    @route("POST", "/moderation/records/{n}/revoke", "erlc.moderate")
    async def erlc_record_revoke(self, ctx: Ctx):
        rec = await self.c.erlc_moderation.get(ctx.guild_id, as_int(ctx.params["n"]))
        if rec and rec["action"] in {"ban", "tempban"}:
            ctx.need("erlc.ban")
        return await self.c.erlc_moderation.revoke(ctx.guild_id, as_int(ctx.params["n"]), ctx.user_id, ctx.b("reason") or "",
                                                   execute=bool(ctx.b("execute")))

    @route("GET", "/moderation/player/{key}", "erlc.view")
    async def erlc_player(self, ctx: Ctx):
        p = await self.c.erlc_moderation.profile(ctx.guild_id, ctx.params["key"], include_discord_cases="mod.view" in ctx.perms)
        names = await self._names(ctx.guild_id, [r["moderator_id"] for r in p["records"]] + [n["author_id"] for n in p["notes"] or []]
                                  + ([p["discord_id"]] if p["discord_id"] else []))
        for r in p["records"]:
            r["moderator"] = names.get(r["moderator_id"] or "")
        for n in p["notes"] or []:
            n["author"] = names.get(n["author_id"])
        p["discord"] = names.get(p["discord_id"] or "")
        p["can"] = {k: k in ctx.perms for k in ("erlc.moderate", "erlc.ban", "bolo.manage", "members.view")}
        return p

    @route("POST", "/moderation/player/{key}/notes", "erlc.moderate")
    async def erlc_player_note(self, ctx: Ctx):
        return await self.c.erlc_moderation.add_note(ctx.guild_id, ctx.params["key"], ctx.user_id, ctx.b("note") or "")

    @route("DELETE", "/moderation/player-notes/{nid}", "erlc.moderate")
    async def erlc_player_note_delete(self, ctx: Ctx):
        if not await self.c.erlc_moderation.delete_note(ctx.guild_id, as_int(ctx.params["nid"]), ctx.user_id):
            raise ApiError(404, "Note not found.")
        return {"ok": True}

    @route("GET", "/moderation/active", "dashboard.view")
    async def moderation_active(self, ctx: Ctx):
        self._need_any(ctx, "mod.view", "erlc.view")
        data = await self.c.erlc_moderation.active_punishments(ctx.guild_id)
        if "erlc.view" not in ctx.perms:
            data["in_game"] = None
        if "mod.view" not in ctx.perms:
            data["discord"] = None
        ids = [r["moderator_id"] for r in (data["in_game"] or []) + (data["discord"] or [])]
        names = await self._names(ctx.guild_id, ids)
        for r in (data["in_game"] or []) + (data["discord"] or []):
            r["moderator"] = names.get(r["moderator_id"] or "")
        return data

    @route("GET", "/moderation/staff", "dashboard.view")
    async def moderation_staff(self, ctx: Ctx):
        self._need_any(ctx, "mod.view", "erlc.view")
        rows = await self.c.erlc_moderation.staff_records(ctx.guild_id, ctx.qi("days", 30))
        if "mod.view" not in ctx.perms:
            for r in rows:
                r["total"] -= sum(r["discord"].values())
                r["discord"] = {}
            rows = [r for r in rows if r["total"]]
        if "erlc.view" not in ctx.perms:
            for r in rows:
                r["total"] -= sum(r["in_game"].values())
                r["in_game"] = {}
            rows = [r for r in rows if r["total"]]
        return {"items": rows, "days": ctx.qi("days", 30)}

    # ================================================================== BOLOs
    def _bolo_out(self, b: dict, names: Optional[dict] = None) -> dict:
        d = dict(b)
        for k in ("created_by", "cleared_by", "roblox_id"):
            d[k] = _sid(d.get(k))
        if names is not None:
            d["creator"] = names.get(d["created_by"] or "")
            d["closer"] = names.get(d["cleared_by"] or "")
        return d

    async def _bolo(self, ctx: Ctx) -> dict:
        b = await self.core.bolos.get_by_number(ctx.guild_id, as_int(ctx.params["n"], "BOLO number"))
        if not b:
            raise ApiError(404, BOLO_NOT_FOUND)
        return b

    @route("GET", "/bolos", "bolo.view")
    async def bolos(self, ctx: Ctx):
        from dispatch.bolo import BOLO_KINDS, BOLO_PRIORITIES
        data = await self.core.bolos.search(ctx.guild_id, status=ctx.q("status", "active"), q=ctx.q("q").strip(), priority=ctx.q("priority"),
                                            kind=ctx.q("kind"), page=ctx.qi("page"))
        names = await self._names(ctx.guild_id, [i["created_by"] for i in data["items"]])
        data["items"] = [self._bolo_out(i, names) for i in data["items"]]
        data["counts"] = {r["priority"]: r["n"] for r in await self.bot.db.fetchall(
            "SELECT priority, COUNT(*) AS n FROM bolos WHERE guild_id = ? AND status = 'active' GROUP BY priority", (ctx.guild_id,))}
        data["counts"]["active"] = sum(data["counts"].values())
        data["counts"]["closed"] = await self.bot.db.fetchval("SELECT COUNT(*) FROM bolos WHERE guild_id = ? AND status != 'active'", (ctx.guild_id,), 0)
        data["priorities"], data["kinds"] = list(BOLO_PRIORITIES), list(BOLO_KINDS)
        data["can_manage"] = "bolo.manage" in ctx.perms
        return data

    async def _bolo_player(self, ctx: Ctx) -> tuple[Optional[int], str]:
        player = str(ctx.b("player") or "").strip()
        if not player:
            return None, ""
        t = await self.c.erlc_moderation.resolve_player(ctx.guild_id, player)
        return t["roblox_id"], t["username"]

    @route("POST", "/bolos", "bolo.manage")
    async def bolo_create(self, ctx: Ctx):
        rid, uname = await self._bolo_player(ctx)
        kind = str(ctx.b("kind") or ("player" if uname else "person"))
        subject = ctx.b("subject") or uname
        b = await self.core.bolos.create(ctx.guild_id, ctx.user_id, kind=kind, subject=subject or "", description=ctx.b("description") or "",
                                         plate=ctx.b("plate") or "", last_seen=ctx.b("last_seen") or "", reason=ctx.b("reason") or "",
                                         priority=str(ctx.b("priority") or "medium"), roblox_id=rid, roblox_username=uname)
        return self._bolo_out(b)

    @route("GET", "/bolos/{n}", "bolo.view")
    async def bolo(self, ctx: Ctx):
        b = await self._bolo(ctx)
        notes = await self.core.bolos.notes(b["id"])
        history = await self.core.bolos.history(b["id"])
        names = await self._names(ctx.guild_id, [b["created_by"], b["cleared_by"]] + [n["author_id"] for n in notes] + [h["actor_id"] for h in history])
        out = self._bolo_out(b, names)
        out["notes"] = [{**n, "author_id": str(n["author_id"]), "author": names.get(str(n["author_id"]))} for n in notes]
        out["history"] = [{"event": h["event_type"], "at": h["at"], "actor_id": _sid(h["actor_id"]), "actor": names.get(str(h["actor_id"])),
                           "detail": h["detail"]} for h in history]
        out["player_online"] = None
        if self.live and (b.get("roblox_id") or b.get("roblox_username")):
            st = self.live.state(ctx.guild_id)
            if st.updated_at:
                out["player_online"] = any(p["roblox_id"] == str(b.get("roblox_id")) or p["name"].lower() == (b.get("roblox_username") or "").lower()
                                           for p in st.players)
        out["can_manage"] = "bolo.manage" in ctx.perms
        return out

    @route("PATCH", "/bolos/{n}", "bolo.manage")
    async def bolo_update(self, ctx: Ctx):
        b = await self._bolo(ctx)
        fields: dict[str, Any] = {k: ctx.b(k) for k in ("subject", "description", "plate", "last_seen", "reason", "priority", "kind") if k in ctx.body}
        if "player" in ctx.body:
            rid, uname = await self._bolo_player(ctx)
            fields.update(roblox_id=rid, roblox_username=uname)
        try:
            return self._bolo_out(await self.core.bolos.update(b["id"], ctx.user_id, **fields))
        except ValueError as e:
            raise ApiError(400, str(e)) from e

    @route("POST", "/bolos/{n}/close", "bolo.manage")
    async def bolo_close(self, ctx: Ctx):
        b = await self._bolo(ctx)
        try:
            return self._bolo_out(await self.core.bolos.clear(b["id"], ctx.user_id, ctx.b("reason") or ""))
        except ValueError as e:
            raise ApiError(400, str(e)) from e

    @route("POST", "/bolos/{n}/reopen", "bolo.manage")
    async def bolo_reopen(self, ctx: Ctx):
        b = await self._bolo(ctx)
        try:
            return self._bolo_out(await self.core.bolos.reopen(b["id"], ctx.user_id))
        except ValueError as e:
            raise ApiError(400, str(e)) from e

    @route("POST", "/bolos/{n}/notes", "bolo.view")
    async def bolo_note(self, ctx: Ctx):
        self._need_any(ctx, "bolo.manage", "erlc.moderate")
        b = await self._bolo(ctx)
        return {"id": await self.core.bolos.add_note(b["id"], ctx.user_id, ctx.b("note") or "")}

    # ================================================================== staff
    @route("GET", "/staff/overview", "staff.view")
    async def staff_overview(self, ctx: Ctx):
        out = await self.c.staff.overview(ctx.guild_id)
        out["can"] = {k: k in ctx.perms for k in ("staff.manage", "shift.manage", "staff.leave_review", "staff.notes")}
        return out

    @route("GET", "/staff/leave/mine", "shift.self")
    async def leave_mine(self, ctx: Ctx):
        rows = row_dicts(await self.bot.db.fetchall("SELECT * FROM leave_requests WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 20",
                                                    (ctx.guild_id, ctx.user_id)))
        for r in rows:
            r["user_id"], r["reviewed_by"] = str(r["user_id"]), _sid(r["reviewed_by"])
        staff = await self.c.staff.get(ctx.guild_id, ctx.user_id)
        return {"items": rows, "is_staff": bool(staff and staff["status"] != "removed")}

    @route("POST", "/staff/leave/request", "shift.self")
    async def leave_request(self, ctx: Ctx):
        r = await self.c.staff.request_leave(ctx.guild_id, ctx.user_id, str(ctx.b("starts_on") or ""), str(ctx.b("ends_on") or ""),
                                             ctx.b("reason") or "")
        r["user_id"] = str(r["user_id"])
        return r

    @route("POST", "/staff/leave/{lid}/cancel", "shift.self")
    async def leave_cancel(self, ctx: Ctx):
        r = await self.c.staff.cancel_leave(ctx.guild_id, as_int(ctx.params["lid"]), ctx.user_id)
        r["user_id"] = str(r["user_id"])
        return r

    # ================================================================== health (overview extras)
    async def platform_health(self, ctx: Ctx) -> dict:
        bot = self.bot
        ready = bot.is_ready() if hasattr(bot, "is_ready") else None
        web = getattr(bot, "web", None)
        streams = sum(len(q) for q in getattr(web, "_streams", {}).values()) if web else 0
        conn = await self._sync_state(ctx.guild_id)
        snap = self.live.snapshot(ctx.guild_id) if self.live else None
        return {"discord": {"ok": bool(ready) if ready is not None else None},
                "api": {"ok": True, "live_clients": streams, "time": iso_now()},
                "erlc": {"status": conn["status"], "connected": conn["connected"], "detail": conn["detail"],
                         "players": snap["current_players"] if snap else None, "staff_online": len(snap["staff_online"]) if snap else None,
                         "age_s": snap["age_s"] if snap else None}}

    async def activity_trend(self, gid: int, days: int = 14) -> dict:
        start = (utcnow() - dt.timedelta(days=days - 1)).date()
        labels = [(start + dt.timedelta(days=i)).isoformat() for i in range(days)]
        series = {k: dict.fromkeys(labels, 0) for k in ("activity", "joins", "moderation")}
        for r in await self.bot.db.fetchall("SELECT substr(at,1,10) AS d, COUNT(*) AS n FROM activity_events WHERE guild_id = ? AND at >= ? "
                                            "AND category != 'erlc_players' GROUP BY d", (gid, labels[0])):
            if r["d"] in series["activity"]:
                series["activity"][r["d"]] = r["n"]
        for r in await self.bot.db.fetchall("SELECT substr(at,1,10) AS d, COUNT(*) AS n FROM erlc_player_events WHERE guild_id = ? AND at >= ? "
                                            "AND kind = 'join' GROUP BY d", (gid, labels[0])):
            if r["d"] in series["joins"]:
                series["joins"][r["d"]] = r["n"]
        for table in ("erlc_mod_records", "mod_cases"):
            for r in await self.bot.db.fetchall(f"SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM {table} WHERE guild_id = ? AND "
                                                f"created_at >= ? GROUP BY d", (gid, labels[0])):
                if r["d"] in series["moderation"]:
                    series["moderation"][r["d"]] += r["n"]
        return {"labels": labels, "series": {k: list(v.values()) for k, v in series.items()}}
