"""Staff management (directory, ranks, promotions, notes, leave) and staff shifts."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Optional

from .common import (ServiceError, ValidationError, clean_text, fmt_duration, iso_now, paginate_sql, parse_date, parse_iso,
                     row_dicts, utcnow)

if TYPE_CHECKING:
    from .platform import CommunityPlatform

STAFF_DEFAULTS = {
    "ranks": [],                # ordered low -> high: [{"name": "Trial Moderator", "role_id": 123}, ...]
    "departments": [],          # free list of staff departments (e.g. "Moderation", "Management")
    "inactivity_days": 0,       # 0 = off; notify when an active staff member has no shift for this many days
    "leave_role_id": None,      # given while on approved leave
}
SHIFT_DEFAULTS = {
    "types": ["General", "Patrol", "Dispatch", "Fire/EMS", "Moderation"],
    "on_duty_role_id": None,    # given while a shift is running
    "max_hours": 12,            # shifts longer than this are closed automatically
}


class StaffService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "staff_settings", STAFF_DEFAULTS)

    async def update_settings(self, guild_id: int, changes: dict, actor_id: int) -> dict:
        clean = {}
        if "ranks" in changes:
            ranks = []
            for r in changes["ranks"] or []:
                name = clean_text((r or {}).get("name"), 40)
                if name:
                    rid = r.get("role_id")
                    ranks.append({"name": name, "role_id": int(rid) if rid not in (None, "", 0, "0") else None})
            if len({r["name"].lower() for r in ranks}) != len(ranks):
                raise ValidationError("Rank names must be unique.")
            clean["ranks"] = ranks[:25]
        if "departments" in changes:
            clean["departments"] = [d for d in (clean_text(x, 40) for x in changes["departments"] or []) if d][:25]
        if "inactivity_days" in changes:
            clean["inactivity_days"] = max(0, min(365, int(changes["inactivity_days"] or 0)))
        if "leave_role_id" in changes:
            v = changes["leave_role_id"]
            clean["leave_role_id"] = int(v) if v not in (None, "", 0, "0") else None
        return await self.p.update_settings(guild_id, "staff_settings", STAFF_DEFAULTS, clean, actor_id)

    # ------------------------------------------------------------------ directory
    async def get(self, guild_id: int, user_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM staff WHERE guild_id = ? AND user_id = ?", (guild_id, int(user_id)))
        if not r:
            return None
        d = dict(r)
        d["user_id"] = str(d["user_id"])
        return d

    async def list(self, guild_id: int, *, status: str = "current", q: str = "", department: str = "",
                   page: int = 1, per_page: int = 50) -> dict:
        where, params = ["s.guild_id = ?"], [guild_id]
        if status == "current":
            where.append("s.status IN ('active', 'leave')")
        elif status in {"active", "leave", "removed"}:
            where.append("s.status = ?")
            params.append(status)
        if department:
            where.append("s.department = ?")
            params.append(department)
        if q:
            where.append("(m.username LIKE ? OR m.display_name LIKE ? OR s.rank LIKE ? OR CAST(s.user_id AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", f"%{q}%", q]
        base = (" FROM staff s LEFT JOIN members m ON m.guild_id = s.guild_id AND m.user_id = s.user_id WHERE " + " AND ".join(where))
        select = ("SELECT s.*, m.username, m.display_name, m.avatar_url, "
                  "(SELECT MAX(started_at) FROM shifts sh WHERE sh.guild_id = s.guild_id AND sh.user_id = s.user_id) AS last_shift, "
                  "(SELECT COUNT(*) FROM shifts sh WHERE sh.guild_id = s.guild_id AND sh.user_id = s.user_id AND sh.ended_at IS NULL) AS on_shift"
                  + base + " ORDER BY s.status, s.rank, lower(COALESCE(m.display_name, m.username))")
        pg = await paginate_sql(self.db, select, "SELECT COUNT(*)" + base, params, page, per_page)
        ranks = [r["name"] for r in (await self.settings(guild_id))["ranks"]]
        for it in pg.items:
            it["user_id"] = str(it["user_id"])
            it["rank_index"] = ranks.index(it["rank"]) if it["rank"] in ranks else -1
        return pg.as_dict()

    async def count(self, guild_id: int) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM staff WHERE guild_id = ? AND status IN ('active','leave')", (guild_id,), 0)

    async def profile(self, guild_id: int, user_id: int, *, include_notes: bool) -> dict:
        s = await self.get(guild_id, user_id)
        if not s:
            raise ServiceError("That member is not on the staff team.")
        out = {"staff": s,
               "member": await self.p.members.get(guild_id, int(user_id)),
               "history": row_dicts(await self.db.fetchall("SELECT * FROM staff_history WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 50",
                                                          (guild_id, int(user_id)))),
               "shifts": await self.p.shifts.summary(guild_id, int(user_id)),
               "recent_shifts": (await self.p.shifts.history(guild_id, user_id=int(user_id), per_page=10))["items"],
               "moderation_actions": row_dicts(await self.db.fetchall(
                   "SELECT number, action, target_id, reason, created_at FROM mod_cases WHERE guild_id = ? AND moderator_id = ? "
                   "ORDER BY id DESC LIMIT 15", (guild_id, int(user_id)))),
               "leave": row_dicts(await self.db.fetchall("SELECT * FROM leave_requests WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 10",
                                                        (guild_id, int(user_id)))),
               "applications": row_dicts(await self.db.fetchall(
                   "SELECT number, form_name, status, created_at FROM applications WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 10",
                   (guild_id, int(user_id))))}
        for c in out["moderation_actions"]:
            c["target_id"] = str(c["target_id"])
        if include_notes:
            out["notes"] = row_dicts(await self.db.fetchall("SELECT * FROM staff_notes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
                                                           (guild_id, int(user_id))))
        return out

    # ------------------------------------------------------------------ changes
    async def _rank_role(self, guild_id: int, rank: Optional[str]) -> Optional[int]:
        for r in (await self.settings(guild_id))["ranks"]:
            if rank and r["name"].lower() == rank.lower():
                return r.get("role_id")
        return None

    async def _sync_rank_roles(self, guild_id: int, user_id: int, old: Optional[str], new: Optional[str]) -> list[str]:
        problems = []
        old_role, new_role = await self._rank_role(guild_id, old), await self._rank_role(guild_id, new)
        if old_role and old_role != new_role:
            if (err := await self.p.actions.remove_role(guild_id, user_id, old_role, f"Staff rank change: {old} -> {new}")):
                problems.append(f"remove {old} role: {err}")
        if new_role:
            if (err := await self.p.actions.add_role(guild_id, user_id, new_role, f"Staff rank: {new}")):
                problems.append(f"give {new} role: {err}")
        return problems

    async def _record(self, guild_id, user_id, action, actor_id, from_rank=None, to_rank=None, reason=None):
        await self.db.execute("INSERT INTO staff_history(guild_id, user_id, action, from_rank, to_rank, actor_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (guild_id, int(user_id), action, from_rank, to_rank, actor_id, clean_text(reason, 500) or None))
        await self.p.audit.record(guild_id, actor_id, f"staff.{action}", str(user_id), {"from": from_rank, "to": to_rank})
        await self.p.publish("staff.changed", guild_id, user_id=int(user_id), action=action, rank=to_rank, actor_id=actor_id)

    async def appoint(self, guild_id: int, user_id: int, rank: str, actor_id: int, *, department: str = "", reason: str = "") -> dict:
        rank = clean_text(rank, 40) or ((await self.settings(guild_id))["ranks"] or [{"name": "Staff"}])[0]["name"]
        existing = await self.get(guild_id, user_id)
        if existing and existing["status"] != "removed":
            raise ServiceError("That member is already on the staff team. Use promote/demote or edit instead.")
        dept = clean_text(department, 40) or None
        if existing:
            await self.db.execute("UPDATE staff SET rank = ?, department = ?, status = 'active', appointed_at = ?, appointed_by = ?, "
                                  "removed_at = NULL, updated_at = ? WHERE id = ?",
                                  (rank, dept, iso_now(), actor_id, iso_now(), int(existing["id"])))
            action = "reinstated"
        else:
            await self.db.execute("INSERT INTO staff(guild_id, user_id, rank, department, appointed_by) VALUES (?, ?, ?, ?, ?)",
                                  (guild_id, int(user_id), rank, dept, actor_id))
            action = "appointed"
        problems = await self._sync_rank_roles(guild_id, int(user_id), None, rank)
        await self._record(guild_id, user_id, action, actor_id, None, rank, reason)
        await self.p.log(guild_id, "staff", f"Staff {action.title()}", fields=[("Member", f"<@{user_id}>", True), ("Rank", rank, True)]
                         + ([("Department", dept, True)] if dept else []) + ([("Problems", "; ".join(problems), False)] if problems else []),
                         actor_id=actor_id, colour=0x2EA043)
        return {"staff": await self.get(guild_id, user_id), "problems": problems}

    async def change_rank(self, guild_id: int, user_id: int, actor_id: int, *, direction: Optional[str] = None,
                          rank: Optional[str] = None, reason: str = "") -> dict:
        s = await self.get(guild_id, user_id)
        if not s or s["status"] == "removed":
            raise ServiceError("That member is not on the staff team.")
        ranks = [r["name"] for r in (await self.settings(guild_id))["ranks"]]
        old = s["rank"]
        if rank:
            new = clean_text(rank, 40, required=True, field="Rank")
        elif direction in {"up", "down"}:
            if old not in ranks:
                raise ServiceError("Set up the rank ladder (Staff settings) to promote/demote by one step, or give the new rank.")
            i = ranks.index(old) + (1 if direction == "up" else -1)
            if i < 0 or i >= len(ranks):
                raise ServiceError("Already at the " + ("highest" if direction == "up" else "lowest") + " rank.")
            new = ranks[i]
        else:
            raise ValidationError("Give a new rank or a direction.")
        if new == old:
            raise ServiceError(f"They already have the rank {old}.")
        action = "promoted" if (new in ranks and old in ranks and ranks.index(new) > ranks.index(old)) or direction == "up" else (
            "demoted" if (new in ranks and old in ranks) or direction == "down" else "updated")
        await self.db.execute("UPDATE staff SET rank = ?, updated_at = ? WHERE id = ?", (new, iso_now(), int(s["id"])))
        problems = await self._sync_rank_roles(guild_id, int(user_id), old, new)
        await self._record(guild_id, user_id, action, actor_id, old, new, reason)
        await self.p.log(guild_id, "staff", f"Staff {action.title()}", fields=[("Member", f"<@{user_id}>", True), ("From", old, True),
                                                                            ("To", new, True)]
                         + ([("Reason", clean_text(reason, 300), False)] if reason else [])
                         + ([("Problems", "; ".join(problems), False)] if problems else []), actor_id=actor_id)
        return {"staff": await self.get(guild_id, user_id), "action": action, "problems": problems}

    async def set_department(self, guild_id: int, user_id: int, department: str, actor_id: int) -> dict:
        s = await self.get(guild_id, user_id)
        if not s or s["status"] == "removed":
            raise ServiceError("That member is not on the staff team.")
        dept = clean_text(department, 40) or None
        await self.db.execute("UPDATE staff SET department = ?, updated_at = ? WHERE id = ?", (dept, iso_now(), int(s["id"])))
        await self._record(guild_id, user_id, "updated", actor_id, s["rank"], s["rank"], f"Department: {dept or 'none'}")
        return await self.get(guild_id, user_id)

    async def remove(self, guild_id: int, user_id: int, actor_id: int, reason: str = "") -> dict:
        s = await self.get(guild_id, user_id)
        if not s or s["status"] == "removed":
            raise ServiceError("That member is not on the staff team.")
        await self.db.execute("UPDATE staff SET status = 'removed', removed_at = ?, updated_at = ? WHERE id = ?",
                              (iso_now(), iso_now(), int(s["id"])))
        problems = await self._sync_rank_roles(guild_id, int(user_id), s["rank"], None)
        open_shift = await self.p.shifts.current(guild_id, int(user_id))
        if open_shift:
            await self.p.shifts.end(guild_id, int(user_id), actor_id, notes="Ended: removed from staff")
        await self._record(guild_id, user_id, "removed", actor_id, s["rank"], None, reason)
        await self.p.log(guild_id, "staff", "Staff Removed", fields=[("Member", f"<@{user_id}>", True), ("Rank", s["rank"], True)]
                         + ([("Reason", clean_text(reason, 300), False)] if reason else []), actor_id=actor_id, colour=0xDA3633)
        return {"problems": problems}

    async def add_note(self, guild_id: int, user_id: int, author_id: int, note: str) -> int:
        if not await self.get(guild_id, user_id):
            raise ServiceError("That member is not on the staff team.")
        nid = await self.db.execute("INSERT INTO staff_notes(guild_id, user_id, author_id, note) VALUES (?, ?, ?, ?)",
                                    (guild_id, int(user_id), author_id, clean_text(note, 1000, required=True, field="Note")))
        await self.p.audit.record(guild_id, author_id, "staff.note", str(user_id))
        return nid

    # ------------------------------------------------------------------ leave
    async def request_leave(self, guild_id: int, user_id: int, starts_on: str, ends_on: str, reason: str) -> dict:
        s = await self.get(guild_id, user_id)
        if not s or s["status"] == "removed":
            raise ServiceError("Only staff members can request leave.")
        start, end = parse_date(starts_on, "Start date"), parse_date(ends_on, "End date")
        if end < start:
            raise ValidationError("The end date is before the start date.")
        if end < utcnow().date().isoformat():
            raise ValidationError("That leave is entirely in the past.")
        if await self.db.fetchone("SELECT 1 FROM leave_requests WHERE guild_id = ? AND user_id = ? AND status = 'pending'", (guild_id, int(user_id))):
            raise ServiceError("You already have a leave request waiting for review.")
        lid = await self.db.execute("INSERT INTO leave_requests(guild_id, user_id, starts_on, ends_on, reason) VALUES (?, ?, ?, ?, ?)",
                                    (guild_id, int(user_id), start, end, clean_text(reason, 500, required=True, field="Reason")))
        await self.p.notifications.create(guild_id, "staff", "Leave request", body=f"<@{user_id}> requested leave {start} → {end}.",
                                          link="#/staff/leave", permission="staff.leave_review")
        await self.p.log(guild_id, "staff", "Leave Requested", fields=[("Member", f"<@{user_id}>", True), ("Dates", f"{start} → {end}", True)])
        return dict(await self.db.fetchone("SELECT * FROM leave_requests WHERE id = ?", (lid,)))

    async def review_leave(self, guild_id: int, leave_id: int, approve: bool, actor_id: int) -> dict:
        r = await self.db.fetchone("SELECT * FROM leave_requests WHERE guild_id = ? AND id = ?", (guild_id, int(leave_id)))
        if not r:
            raise ServiceError("Leave request not found.")
        if r["status"] != "pending":
            raise ServiceError("That request was already reviewed.")
        status = "approved" if approve else "denied"
        await self.db.execute("UPDATE leave_requests SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                              (status, actor_id, iso_now(), int(leave_id)))
        await self.p.audit.record(guild_id, actor_id, f"staff.leave_{status}", str(r["user_id"]))
        await self.p.actions.dm(int(r["user_id"]), content=f"Your leave request ({r['starts_on']} → {r['ends_on']}) was **{status}**.")
        await self.p.log(guild_id, "staff", f"Leave {status.title()}", fields=[("Member", f"<@{r['user_id']}>", True),
                                                                            ("Dates", f"{r['starts_on']} → {r['ends_on']}", True)], actor_id=actor_id)
        await self.tick(guild_id)
        return dict(await self.db.fetchone("SELECT * FROM leave_requests WHERE id = ?", (int(leave_id),)))

    async def leave_list(self, guild_id: int, status: str = "") -> list[dict]:
        sql, params = "SELECT l.*, m.username, m.display_name FROM leave_requests l LEFT JOIN members m ON m.guild_id = l.guild_id AND m.user_id = l.user_id WHERE l.guild_id = ?", [guild_id]
        if status:
            sql += " AND l.status = ?"
            params.append(status)
        rows = row_dicts(await self.db.fetchall(sql + " ORDER BY l.id DESC LIMIT 100", params))
        for r in rows:
            r["user_id"] = str(r["user_id"])
        return rows

    async def tick(self, guild_id: int) -> None:
        """Start/end approved leave on its dates; flag inactivity once a week per member."""
        today = utcnow().date().isoformat()
        s = await self.settings(guild_id)
        on_leave = {r["user_id"] for r in await self.db.fetchall(
            "SELECT user_id FROM leave_requests WHERE guild_id = ? AND status = 'approved' AND starts_on <= ? AND ends_on >= ?",
            (guild_id, today, today))}
        for st in await self.db.fetchall("SELECT * FROM staff WHERE guild_id = ? AND status IN ('active','leave')", (guild_id,)):
            uid = st["user_id"]
            if uid in on_leave and st["status"] == "active":
                await self.db.execute("UPDATE staff SET status = 'leave', updated_at = ? WHERE id = ?", (iso_now(), st["id"]))
                await self._record(guild_id, uid, "leave_start", None, st["rank"], st["rank"])
                if s.get("leave_role_id"):
                    await self.p.actions.add_role(guild_id, uid, int(s["leave_role_id"]), "Staff leave started")
            elif uid not in on_leave and st["status"] == "leave":
                await self.db.execute("UPDATE staff SET status = 'active', updated_at = ? WHERE id = ?", (iso_now(), st["id"]))
                await self._record(guild_id, uid, "leave_end", None, st["rank"], st["rank"])
                if s.get("leave_role_id"):
                    await self.p.actions.remove_role(guild_id, uid, int(s["leave_role_id"]), "Staff leave ended")
        days = int(s.get("inactivity_days") or 0)
        if days:
            cutoff = (utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            week = utcnow().strftime("%G-W%V")
            rows = await self.db.fetchall(
                "SELECT s.user_id, s.rank, (SELECT MAX(started_at) FROM shifts sh WHERE sh.guild_id = s.guild_id AND sh.user_id = s.user_id) AS last "
                "FROM staff s WHERE s.guild_id = ? AND s.status = 'active' AND s.appointed_at < ?", (guild_id, cutoff))
            for r in rows:
                if r["last"] is None or r["last"] < cutoff:
                    nid = await self.p.notifications.create(
                        guild_id, "staff", "Inactive staff member",
                        body=f"<@{r['user_id']}> ({r['rank']}) has not started a shift in {days}+ days.", link=f"#/staff/{r['user_id']}",
                        permission="staff.manage", dedupe_key=f"inactive:{r['user_id']}:{week}", window_seconds=8 * 86400)
                    if nid:
                        await self.p.publish("staff.inactive", guild_id, user_id=r["user_id"], days=days, event_key=f"{r['user_id']}:{week}")


class ShiftService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "shift_settings", SHIFT_DEFAULTS)

    async def update_settings(self, guild_id: int, changes: dict, actor_id: int) -> dict:
        clean = {}
        if "types" in changes:
            types = [t for t in (clean_text(x, 30) for x in changes["types"] or []) if t][:20]
            if not types:
                raise ValidationError("Keep at least one shift type.")
            clean["types"] = types
        if "on_duty_role_id" in changes:
            v = changes["on_duty_role_id"]
            clean["on_duty_role_id"] = int(v) if v not in (None, "", 0, "0") else None
        if "max_hours" in changes:
            clean["max_hours"] = max(1, min(48, int(changes["max_hours"] or 12)))
        return await self.p.update_settings(guild_id, "shift_settings", SHIFT_DEFAULTS, clean, actor_id)

    async def current(self, guild_id: int, user_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM shifts WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL", (guild_id, int(user_id)))
        return dict(r) if r else None

    async def start(self, guild_id: int, user_id: int, shift_type: str = "General") -> dict:
        s = await self.settings(guild_id)
        stype = clean_text(shift_type, 30) or s["types"][0]
        if stype not in s["types"]:
            raise ValidationError(f"Unknown shift type. Choose one of: {', '.join(s['types'])}.")
        if await self.current(guild_id, user_id):
            raise ServiceError("You already have a shift running. End it first with `/shift end`.")
        try:
            sid = await self.db.execute("INSERT INTO shifts(guild_id, user_id, shift_type) VALUES (?, ?, ?)", (guild_id, int(user_id), stype))
        except Exception as e:  # unique index: a second start raced the first
            raise ServiceError("You already have a shift running.") from e
        if s.get("on_duty_role_id"):
            await self.p.actions.add_role(guild_id, int(user_id), int(s["on_duty_role_id"]), "Shift started")
        await self.p.log(guild_id, "shifts", "Shift Started", fields=[("Member", f"<@{user_id}>", True), ("Type", stype, True)],
                         actor_id=int(user_id), colour=0x2EA043)
        await self.p.publish("shift.started", guild_id, user_id=int(user_id), shift_id=sid, shift_type=stype)
        return dict(await self.db.fetchone("SELECT * FROM shifts WHERE id = ?", (sid,)))

    async def end(self, guild_id: int, user_id: int, actor_id: int, notes: str = "") -> dict:
        cur = await self.current(guild_id, user_id)
        if not cur:
            raise ServiceError("There is no shift running.")
        start = parse_iso(cur["started_at"]) or utcnow()
        dur = int((utcnow() - start).total_seconds())
        await self.db.execute("UPDATE shifts SET ended_at = ?, duration_s = ?, notes = ?, ended_by = ? WHERE id = ?",
                              (iso_now(), dur, clean_text(notes, 500) or None, actor_id, cur["id"]))
        s = await self.settings(guild_id)
        if s.get("on_duty_role_id"):
            await self.p.actions.remove_role(guild_id, int(user_id), int(s["on_duty_role_id"]), "Shift ended")
        by_other = int(actor_id) != int(user_id) if actor_id else False
        await self.p.log(guild_id, "shifts", "Shift Ended", fields=[("Member", f"<@{user_id}>", True), ("Type", cur["shift_type"], True),
                                                                   ("Duration", fmt_duration(dur), True)]
                         + ([("Notes", clean_text(notes, 300), False)] if notes else [])
                         + ([("Ended by", f"<@{actor_id}>", True)] if by_other else []), actor_id=actor_id)
        await self.p.publish("shift.ended", guild_id, user_id=int(user_id), shift_id=cur["id"], duration_s=dur)
        return dict(await self.db.fetchone("SELECT * FROM shifts WHERE id = ?", (cur["id"],)))

    async def add_note(self, guild_id: int, user_id: int, note: str) -> dict:
        cur = await self.current(guild_id, user_id)
        if not cur:
            raise ServiceError("There is no shift running.")
        text = "\n".join(x for x in [cur.get("notes"), clean_text(note, 300, required=True, field="Note")] if x)[-1000:]
        await self.db.execute("UPDATE shifts SET notes = ? WHERE id = ?", (text, cur["id"]))
        return await self.current(guild_id, user_id)

    async def active(self, guild_id: int) -> list[dict]:
        rows = row_dicts(await self.db.fetchall(
            "SELECT sh.*, m.username, m.display_name, m.avatar_url, s.rank FROM shifts sh "
            "LEFT JOIN members m ON m.guild_id = sh.guild_id AND m.user_id = sh.user_id "
            "LEFT JOIN staff s ON s.guild_id = sh.guild_id AND s.user_id = sh.user_id "
            "WHERE sh.guild_id = ? AND sh.ended_at IS NULL ORDER BY sh.started_at", (guild_id,)))
        now = utcnow()
        for r in rows:
            r["user_id"] = str(r["user_id"])
            r["elapsed_s"] = int((now - (parse_iso(r["started_at"]) or now)).total_seconds())
        return rows

    async def history(self, guild_id: int, *, user_id: Optional[int] = None, since: Optional[str] = None,
                      page: int = 1, per_page: int = 25) -> dict:
        where, params = ["sh.guild_id = ?", "sh.ended_at IS NOT NULL"], [guild_id]
        if user_id:
            where.append("sh.user_id = ?")
            params.append(int(user_id))
        if since:
            where.append("sh.started_at >= ?")
            params.append(since)
        base = (" FROM shifts sh LEFT JOIN members m ON m.guild_id = sh.guild_id AND m.user_id = sh.user_id WHERE " + " AND ".join(where))
        pg = await paginate_sql(self.db, "SELECT sh.*, m.username, m.display_name" + base + " ORDER BY sh.started_at DESC",
                                "SELECT COUNT(*)" + base, params, page, per_page)
        for it in pg.items:
            it["user_id"] = str(it["user_id"])
        return pg.as_dict()

    async def summary(self, guild_id: int, user_id: int) -> dict:
        week = (utcnow() - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        r = await self.db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_s), 0) AS total, "
            "COALESCE(SUM(CASE WHEN started_at >= ? THEN duration_s ELSE 0 END), 0) AS week, MAX(started_at) AS last "
            "FROM shifts WHERE guild_id = ? AND user_id = ? AND ended_at IS NOT NULL", (week, guild_id, int(user_id)))
        cur = await self.current(guild_id, user_id)
        return {"count": r["n"], "total_s": r["total"], "week_s": r["week"], "last_started": r["last"], "current": cur,
                "total_text": fmt_duration(r["total"]), "week_text": fmt_duration(r["week"])}

    async def leaderboard(self, guild_id: int, since: str, limit: int = 10) -> list[dict]:
        rows = row_dicts(await self.db.fetchall(
            "SELECT sh.user_id, m.username, m.display_name, COUNT(*) AS shifts, SUM(sh.duration_s) AS seconds FROM shifts sh "
            "LEFT JOIN members m ON m.guild_id = sh.guild_id AND m.user_id = sh.user_id "
            "WHERE sh.guild_id = ? AND sh.ended_at IS NOT NULL AND sh.started_at >= ? GROUP BY sh.user_id ORDER BY seconds DESC LIMIT ?",
            (guild_id, since, limit)))
        for r in rows:
            r["user_id"] = str(r["user_id"])
            r["hours"] = round((r["seconds"] or 0) / 3600, 1)
        return rows

    async def close_overdue(self, guild_id: int) -> int:
        s = await self.settings(guild_id)
        cutoff = (utcnow() - dt.timedelta(hours=int(s["max_hours"]))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rows = await self.db.fetchall("SELECT user_id FROM shifts WHERE guild_id = ? AND ended_at IS NULL AND started_at < ?", (guild_id, cutoff))
        for r in rows:
            await self.end(guild_id, r["user_id"], 0, notes=f"Closed automatically after {s['max_hours']} hours")
        return len(rows)
