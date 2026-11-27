"""Moderation center: numbered cases for warnings, timeouts, kicks, bans and unbans, plus
member reports. Every action is recorded with an honest outcome (a failed Discord action is
stored as a failed case, never as a success)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .common import (ServiceError, ValidationError, clean_text, fmt_duration, iso_in, iso_now, paginate_sql, parse_duration)

if TYPE_CHECKING:
    from .platform import CommunityPlatform

ACTIONS = {"warn": "Warning", "timeout": "Timeout", "kick": "Kick", "ban": "Ban", "unban": "Unban", "note": "Note"}
ACTION_ICON = {"warn": "⚠️", "timeout": "⏳", "kick": "👢", "ban": "🔨", "unban": "♻️", "note": "📝"}
ACTION_COLOUR = {"warn": 0xD29922, "timeout": 0xF0883E, "kick": 0xF0883E, "ban": 0xDA3633, "unban": 0x2EA043, "note": 0x6E7681}
MAX_TIMEOUT = 28 * 86400  # Discord limit
DEFAULTS = {"dm_on_action": True, "warn_threshold": 0, "warn_threshold_action": "timeout", "warn_threshold_duration": "1h",
            "appeal_info": ""}


class ModerationService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "moderation_settings", DEFAULTS)

    async def update_settings(self, guild_id: int, changes: dict, actor_id: int) -> dict:
        clean = {}
        if "dm_on_action" in changes:
            clean["dm_on_action"] = bool(changes["dm_on_action"])
        if "warn_threshold" in changes:
            clean["warn_threshold"] = max(0, min(50, int(changes["warn_threshold"] or 0)))
        if "warn_threshold_action" in changes:
            if changes["warn_threshold_action"] not in {"timeout", "kick", "ban"}:
                raise ValidationError("The automatic action must be timeout, kick or ban.")
            clean["warn_threshold_action"] = changes["warn_threshold_action"]
        if "warn_threshold_duration" in changes:
            parse_duration(changes["warn_threshold_duration"], max_seconds=MAX_TIMEOUT)
            clean["warn_threshold_duration"] = str(changes["warn_threshold_duration"])
        if "appeal_info" in changes:
            clean["appeal_info"] = clean_text(changes["appeal_info"], 300)
        return await self.p.update_settings(guild_id, "moderation_settings", DEFAULTS, clean, actor_id)

    def _case(self, r) -> dict:
        d = dict(r)
        d["target_id"], d["moderator_id"] = str(d["target_id"]), str(d["moderator_id"])
        return d

    async def get(self, guild_id: int, number: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM mod_cases WHERE guild_id = ? AND number = ?", (guild_id, int(number)))
        return self._case(r) if r else None

    async def history(self, guild_id: int, target_id: int, limit: int = 50) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? ORDER BY id DESC LIMIT ?",
                                      (guild_id, int(target_id), limit))
        return [self._case(r) for r in rows]

    async def list(self, guild_id: int, *, action: str = "", status: str = "", q: str = "", target_id: Optional[int] = None,
                   moderator_id: Optional[int] = None, since: str = "", until: str = "", page: int = 1, per_page: int = 25) -> dict:
        where, params = ["c.guild_id = ?"], [guild_id]
        if action in ACTIONS:
            where.append("c.action = ?")
            params.append(action)
        if status in {"active", "revoked", "failed"}:
            where.append("c.status = ?")
            params.append(status)
        if target_id:
            where.append("c.target_id = ?")
            params.append(int(target_id))
        if moderator_id:
            where.append("c.moderator_id = ?")
            params.append(int(moderator_id))
        if since:
            where.append("c.created_at >= ?")
            params.append(since)
        if until:
            where.append("c.created_at < ?")
            params.append(until)
        if q:
            where.append("(c.reason LIKE ? OR c.target_name LIKE ? OR CAST(c.number AS TEXT) = ? OR CAST(c.target_id AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", q.lstrip("#"), q]
        base = " FROM mod_cases c WHERE " + " AND ".join(where)
        pg = await paginate_sql(self.db, "SELECT c.*" + base + " ORDER BY c.id DESC", "SELECT COUNT(*)" + base, params, page, per_page)
        pg.items = [self._case(i) for i in pg.items]
        return pg.as_dict()

    async def create(self, guild_id: int, action: str, target_id: int, moderator_id: int, *, reason: str, evidence: str = "",
                     duration: Optional[str] = None, source: str = "discord", delete_message_days: int = 0,
                     auto: bool = False) -> dict:
        if action not in ACTIONS:
            raise ValidationError("Unknown moderation action.")
        reason = clean_text(reason, 500, required=action != "note", field="Reason") or "No reason given"
        evidence = clean_text(evidence, 500)
        target_id = int(target_id)
        if target_id == int(moderator_id) and not auto:
            raise ServiceError("You can't moderate yourself.")
        target = self.p.actions.member(guild_id, target_id)
        if action in {"timeout", "kick"} and target is None and self.p.actions.available:
            raise ServiceError("That user is not in this server.")
        if action != "unban" and target is not None and not auto:
            problem = self.p.actions.can_moderate(guild_id, int(moderator_id), target_id)
            if problem:
                raise ServiceError(problem)
        seconds = None
        if action == "timeout":
            seconds = parse_duration(duration or "", max_seconds=MAX_TIMEOUT)
        name = (target or {}).get("name") or (await self.p.members.get(guild_id, target_id) or {}).get("username")
        s = await self.settings(guild_id)
        server = (self.p.actions.guild_info(guild_id) or {}).get("name") or "the server"

        # Tell the member first: after a kick/ban the DM can no longer reach them.
        dm_note = None
        if s["dm_on_action"] and action in {"warn", "timeout", "kick", "ban"}:
            verb = {"warn": "warned", "timeout": f"timed out for {fmt_duration(seconds)}", "kick": "kicked", "ban": "banned"}[action]
            desc = f"You were **{verb}** in **{server}**.\n**Reason:** {reason}"
            if s.get("appeal_info") and action in {"ban", "kick"}:
                desc += f"\n\n{s['appeal_info']}"
            err = await self.p.actions.dm(target_id, embed={"title": f"{ACTION_ICON[action]} Moderation notice", "description": desc,
                                                            "color": ACTION_COLOUR[action], "footer": {"text": "Pulse Dispatch | ERLC"}})
            dm_note = "DM not delivered" if err else "DM delivered"

        audit_reason = f"{reason} — by {moderator_id}"[:500]
        err = None
        if action == "timeout":
            err = await self.p.actions.timeout(guild_id, target_id, seconds, audit_reason)
        elif action == "kick":
            err = await self.p.actions.kick(guild_id, target_id, audit_reason)
        elif action == "ban":
            err = await self.p.actions.ban(guild_id, target_id, audit_reason, max(0, min(7, int(delete_message_days or 0))))
        elif action == "unban":
            err = await self.p.actions.unban(guild_id, target_id, audit_reason)
        status = "failed" if err else "active"
        outcome = "; ".join(x for x in [f"Failed: {err}" if err else "Completed", dm_note] if x)

        number = await self.db.next_number(guild_id, "case")
        await self.db.execute(
            "INSERT INTO mod_cases(guild_id, number, action, target_id, target_name, moderator_id, reason, evidence, duration_s, expires_at, "
            "status, outcome, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, action, target_id, name, int(moderator_id), reason, evidence or None, seconds,
             iso_in(seconds) if seconds else None, status, outcome, source))
        if action == "unban" and not err:
            await self.db.execute("UPDATE mod_cases SET status = 'revoked', updated_at = ? WHERE guild_id = ? AND target_id = ? "
                                  "AND action = 'ban' AND status = 'active'", (iso_now(), guild_id, target_id))
        await self.p.audit.record(guild_id, int(moderator_id), f"mod.{action}", str(target_id), {"case": number, "status": status})
        fields = [("Member", f"<@{target_id}> ({clean_text(name or '', 40) or target_id})", True), ("Case", f"#{number}", True),
                  ("Reason", reason, False)]
        if seconds:
            fields.insert(2, ("Duration", fmt_duration(seconds), True))
        if evidence:
            fields.append(("Evidence", evidence, False))
        fields.append(("Outcome", outcome, False))
        await self.p.log(guild_id, "moderation", f"{ACTION_ICON[action]} {ACTIONS[action]}{' (failed)' if err else ''}", fields=fields,
                         actor_id=int(moderator_id), colour=ACTION_COLOUR[action])
        if action in {"ban", "kick"} and not err:
            await self.p.notifications.create(guild_id, "moderation", f"{ACTIONS[action]}: {name or target_id}",
                                              body=f"Case #{number} by <@{moderator_id}> — {reason}", link=f"#/moderation/{number}",
                                              permission="mod.view")
        await self.p.publish("mod.case", guild_id, case_number=number, action=action, user_id=target_id, moderator_id=int(moderator_id),
                             status=status, reason=reason, origin="automation" if auto else None)
        case = await self.get(guild_id, number)
        if action == "warn" and not err and not auto:
            case["escalation"] = await self._maybe_escalate(guild_id, target_id, moderator_id)
        if err:
            raise ServiceError(f"Case #{number} was recorded, but Discord refused the {ACTIONS[action].lower()}: {err}")
        return case

    async def _maybe_escalate(self, guild_id: int, target_id: int, moderator_id: int) -> Optional[dict]:
        s = await self.settings(guild_id)
        threshold = int(s["warn_threshold"] or 0)
        if not threshold:
            return None
        n = await self.db.fetchval("SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND target_id = ? AND action = 'warn' AND status = 'active'",
                                   (guild_id, target_id), 0)
        if n < threshold or n % threshold:
            return None
        try:
            return await self.create(guild_id, s["warn_threshold_action"], target_id, moderator_id,
                                     reason=f"Automatic: reached {n} active warnings", duration=s["warn_threshold_duration"],
                                     auto=True)
        except ServiceError as e:
            return {"error": str(e)}

    async def revoke(self, guild_id: int, number: int, actor_id: int, reason: str = "") -> dict:
        case = await self.get(guild_id, number)
        if not case:
            raise ServiceError("Case not found.")
        if case["status"] != "active":
            raise ServiceError(f"Case #{number} is already {case['status']}.")
        if case["action"] == "ban":
            raise ServiceError("Use unban to lift a ban (it creates its own case).")
        if case["action"] == "timeout":
            await self.p.actions.remove_timeout(guild_id, int(case["target_id"]), f"Case #{number} revoked")
        await self.db.execute("UPDATE mod_cases SET status = 'revoked', outcome = outcome || ?, updated_at = ? WHERE id = ?",
                              (f"; revoked by {actor_id}: {clean_text(reason, 200)}", iso_now(), case["id"]))
        await self.p.audit.record(guild_id, actor_id, "mod.revoke", f"case:{number}")
        await self.p.log(guild_id, "moderation", f"Case #{number} Revoked", fields=[("Member", f"<@{case['target_id']}>", True),
                                                                                 ("Action", ACTIONS[case["action"]], True)]
                         + ([("Reason", clean_text(reason, 300), False)] if reason else []), actor_id=actor_id)
        return await self.get(guild_id, number)

    async def edit(self, guild_id: int, number: int, actor_id: int, *, reason: Optional[str] = None, evidence: Optional[str] = None) -> dict:
        case = await self.get(guild_id, number)
        if not case:
            raise ServiceError("Case not found.")
        await self.db.execute("UPDATE mod_cases SET reason = COALESCE(?, reason), evidence = COALESCE(?, evidence), updated_at = ? WHERE id = ?",
                              (clean_text(reason, 500) or None if reason is not None else None,
                               clean_text(evidence, 500) or None if evidence is not None else None, iso_now(), case["id"]))
        await self.p.audit.record(guild_id, actor_id, "mod.edit", f"case:{number}")
        return await self.get(guild_id, number)

    # ------------------------------------------------------------------ member reports
    async def report(self, guild_id: int, reporter_id: int, target_id: int, reason: str, evidence: str = "") -> dict:
        if int(reporter_id) == int(target_id):
            raise ServiceError("You can't report yourself.")
        reason = clean_text(reason, 1000, required=True, field="Reason")
        if await self.db.fetchone("SELECT 1 FROM member_reports WHERE guild_id = ? AND reporter_id = ? AND target_id = ? AND status = 'open'",
                                  (guild_id, int(reporter_id), int(target_id))):
            raise ServiceError("You already have an open report about this member. Staff will look at it soon.")
        number = await self.db.next_number(guild_id, "member_report")
        await self.db.execute("INSERT INTO member_reports(guild_id, number, reporter_id, target_id, reason, evidence) VALUES (?, ?, ?, ?, ?, ?)",
                              (guild_id, number, int(reporter_id), int(target_id), reason, clean_text(evidence, 500) or None))
        await self.p.log(guild_id, "moderation", f"Member Report #{number}", fields=[("Reported", f"<@{target_id}>", True),
                                                                                  ("By", f"<@{reporter_id}>", True), ("Reason", reason, False)])
        await self.p.notifications.create(guild_id, "report", f"Member report #{number}", body=reason[:200], link="#/moderation/reports",
                                          permission="mod.reports_manage")
        await self.p.publish("mod.report", guild_id, report_number=number, user_id=int(target_id), reporter_id=int(reporter_id))
        return dict(await self.db.fetchone("SELECT * FROM member_reports WHERE guild_id = ? AND number = ?", (guild_id, number)))

    async def reports(self, guild_id: int, status: str = "open", page: int = 1, per_page: int = 25) -> dict:
        where, params = "WHERE guild_id = ?", [guild_id]
        if status in {"open", "actioned", "dismissed"}:
            where += " AND status = ?"
            params.append(status)
        pg = await paginate_sql(self.db, f"SELECT * FROM member_reports {where} ORDER BY id DESC", f"SELECT COUNT(*) FROM member_reports {where}",
                                params, page, per_page)
        for it in pg.items:
            it["reporter_id"], it["target_id"] = str(it["reporter_id"]), str(it["target_id"])
        return pg.as_dict()

    async def resolve_report(self, guild_id: int, number: int, actor_id: int, status: str, resolution: str = "",
                             case_number: Optional[int] = None) -> dict:
        if status not in {"actioned", "dismissed"}:
            raise ValidationError("Status must be actioned or dismissed.")
        r = await self.db.fetchone("SELECT * FROM member_reports WHERE guild_id = ? AND number = ?", (guild_id, int(number)))
        if not r:
            raise ServiceError("Report not found.")
        if r["status"] != "open":
            raise ServiceError("That report was already handled.")
        await self.db.execute("UPDATE member_reports SET status = ?, handled_by = ?, handled_at = ?, resolution = ?, case_number = ? WHERE id = ?",
                              (status, actor_id, iso_now(), clean_text(resolution, 500) or None, case_number, r["id"]))
        await self.p.audit.record(guild_id, actor_id, f"mod.report_{status}", f"report:{number}")
        await self.p.actions.dm(int(r["reporter_id"]), content=f"Thanks for your report #{number}. Staff marked it as **{status}**.")
        return dict(await self.db.fetchone("SELECT * FROM member_reports WHERE id = ?", (r["id"],)))

    async def counts(self, guild_id: int, since_iso: str) -> dict:
        rows = await self.db.fetchall("SELECT action, COUNT(*) AS n FROM mod_cases WHERE guild_id = ? AND created_at >= ? GROUP BY action",
                                      (guild_id, since_iso))
        out = {r["action"]: r["n"] for r in rows}
        out["open_reports"] = await self.db.fetchval("SELECT COUNT(*) FROM member_reports WHERE guild_id = ? AND status = 'open'", (guild_id,), 0)
        return out

