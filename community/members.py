"""Member directory: a local mirror of Discord members (kept current from gateway events),
plus member notes, join/leave history and the welcome message.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

from database.models import dumps

from .common import ServiceError, clean_text, iso_now, loads, paginate_sql, row_dicts

if TYPE_CHECKING:
    from .platform import CommunityPlatform

WELCOME_DEFAULTS = {"enabled": False, "channel_id": None, "template_id": None, "message": "Welcome to {server}, {user}!",
                    "dm": False, "auto_role_ids": [], "leave_enabled": False, "leave_channel_id": None,
                    "leave_message": "**{user_name}** has left the server."}

FILTERS = {"all", "verified", "unverified", "staff", "regular", "moderated", "department", "left"}


class MemberService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    # ------------------------------------------------------------------ sync from Discord
    async def upsert(self, guild_id: int, m: dict, *, joined: bool = False) -> None:
        """m: {id, name, display_name, avatar_url, bot, roles, joined_at}."""
        await self.db.execute(
            "INSERT INTO members(guild_id, user_id, username, display_name, avatar_url, is_bot, roles, joined_at, left_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET username = excluded.username, "
            "display_name = excluded.display_name, avatar_url = excluded.avatar_url, is_bot = excluded.is_bot, roles = excluded.roles, "
            "joined_at = COALESCE(excluded.joined_at, members.joined_at), left_at = NULL, updated_at = excluded.updated_at",
            (guild_id, int(m["id"]), m.get("name"), m.get("display_name"), m.get("avatar_url"), int(bool(m.get("bot"))),
             dumps(sorted(int(r) for r in m.get("roles") or [])), m.get("joined_at"), iso_now()))
        if joined:
            await self.db.execute("INSERT INTO member_events(guild_id, user_id, kind) VALUES (?, ?, 'join')", (guild_id, int(m["id"])))

    async def bulk_sync(self, guild_id: int, members: Iterable[dict]) -> int:
        """Startup sync: mirror every current member and mark everyone else as gone (no join/leave events)."""
        seen = []
        async with self.db.transaction() as c:
            now = iso_now()
            for m in members:
                seen.append(int(m["id"]))
                await c.execute(
                    "INSERT INTO members(guild_id, user_id, username, display_name, avatar_url, is_bot, roles, joined_at, left_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET username = excluded.username, "
                    "display_name = excluded.display_name, avatar_url = excluded.avatar_url, is_bot = excluded.is_bot, "
                    "roles = excluded.roles, joined_at = COALESCE(excluded.joined_at, members.joined_at), left_at = NULL, "
                    "updated_at = excluded.updated_at",
                    (guild_id, int(m["id"]), m.get("name"), m.get("display_name"), m.get("avatar_url"), int(bool(m.get("bot"))),
                     dumps(sorted(int(r) for r in m.get("roles") or [])), m.get("joined_at"), now))
            if seen:
                marks = ",".join("?" * len(seen))
                await c.execute(f"UPDATE members SET left_at = ? WHERE guild_id = ? AND left_at IS NULL AND user_id NOT IN ({marks})",
                                (now, guild_id, *seen))
        return len(seen)

    async def mark_left(self, guild_id: int, user_id: int) -> None:
        await self.db.execute("UPDATE members SET left_at = ? WHERE guild_id = ? AND user_id = ?", (iso_now(), guild_id, user_id))
        await self.db.execute("INSERT INTO member_events(guild_id, user_id, kind) VALUES (?, ?, 'leave')", (guild_id, user_id))

    async def update_roles(self, guild_id: int, user_id: int, roles: Iterable[int]) -> None:
        await self.db.execute("UPDATE members SET roles = ?, updated_at = ? WHERE guild_id = ? AND user_id = ?",
                              (dumps(sorted(int(r) for r in roles)), iso_now(), guild_id, user_id))

    # ------------------------------------------------------------------ welcome
    async def welcome_settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "welcome_settings", WELCOME_DEFAULTS)

    async def on_join(self, guild_id: int, m: dict) -> list[str]:
        """Mirror, log, welcome and auto-role a new member. Returns any problems (for the log)."""
        await self.upsert(guild_id, m, joined=True)
        problems: list[str] = []
        s = await self.welcome_settings(guild_id)
        uid = int(m["id"])
        if s["enabled"] and not m.get("bot"):
            vals = await self.p.embeds.values(guild_id, uid)
            embed = None
            if s.get("template_id"):
                t = await self.p.embeds.get(guild_id, s["template_id"])
                if t:
                    from .embeds import render
                    embed = render(t["data"], vals)
            from .common import safe_format
            text = safe_format(s.get("message") or "", vals) if not embed else (f"<@{uid}>" if s.get("channel_id") else None)
            if s.get("dm"):
                err = await self.p.actions.dm(uid, content=text if not embed else None, embed=embed)
                if err:
                    problems.append(f"Welcome DM: {err}")
            if s.get("channel_id"):
                _, err = await self.p.actions.send(guild_id, int(s["channel_id"]), content=text, embed=embed)
                if err:
                    problems.append(f"Welcome message: {err}")
        for rid in s.get("auto_role_ids") or []:
            err = await self.p.actions.add_role(guild_id, uid, int(rid), "Hive Dispatch auto-role")
            if err:
                problems.append(f"Auto-role {rid}: {err}")
        await self.p.log(guild_id, "members", "Member Joined",
                         fields=[("Member", f"<@{uid}> ({clean_text(m.get('name'), 60)})", True), ("ID", str(uid), True)]
                         + ([("Problems", "\n".join(problems)[:500], False)] if problems else []))
        await self.p.publish("member.joined", guild_id, user_id=uid, user_name=m.get("name"))
        return problems

    async def on_leave(self, guild_id: int, user_id: int, name: str = "") -> None:
        await self.mark_left(guild_id, user_id)
        s = await self.welcome_settings(guild_id)
        if s.get("leave_enabled") and s.get("leave_channel_id"):
            from .common import safe_format
            text = safe_format(s.get("leave_message") or "", {"user_name": name or str(user_id), "user_id": user_id,
                                                              "user": f"<@{user_id}>"})
            await self.p.actions.send(guild_id, int(s["leave_channel_id"]), content=text)
        await self.p.log(guild_id, "members", "Member Left", fields=[("Member", f"<@{user_id}> ({clean_text(name, 60)})", True),
                                                                   ("ID", str(user_id), True)])
        await self.p.publish("member.left", guild_id, user_id=user_id, user_name=name)

    # ------------------------------------------------------------------ directory
    async def directory(self, guild_id: int, *, q: str = "", filter: str = "all", department: str = "",
                        page: int = 1, per_page: int = 25) -> dict:
        f = filter if filter in FILTERS else "all"
        where = ["m.guild_id = ?", "m.is_bot = 0"]
        params: list = [guild_id]
        where.append("m.left_at IS NOT NULL" if f == "left" else "m.left_at IS NULL")
        if q:
            where.append("(m.username LIKE ? OR m.display_name LIKE ? OR CAST(m.user_id AS TEXT) = ? OR rl.roblox_username LIKE ?)")
            like = f"%{q.strip()}%"
            params += [like, like, q.strip(), like]
        if f == "verified":
            where.append("rl.roblox_id IS NOT NULL")
        elif f == "unverified":
            where.append("rl.roblox_id IS NULL")
        elif f == "staff":
            where.append("s.status IN ('active', 'leave')")
        elif f == "regular":
            where.append("(s.status IS NULL OR s.status = 'removed')")
        elif f == "moderated":
            where.append("EXISTS (SELECT 1 FROM mod_cases c WHERE c.guild_id = m.guild_id AND c.target_id = m.user_id)")
        elif f == "department" and department:
            where.append("(s.department = ? OR EXISTS (SELECT 1 FROM officers o JOIN departments d ON d.id = o.department_id "
                         "WHERE o.guild_id = m.guild_id AND o.discord_id = m.user_id AND o.active = 1 AND d.short_code = ?))")
            params += [department, department]
        base = (" FROM members m LEFT JOIN roblox_links rl ON rl.guild_id = m.guild_id AND rl.discord_id = m.user_id "
                "LEFT JOIN staff s ON s.guild_id = m.guild_id AND s.user_id = m.user_id WHERE " + " AND ".join(where))
        select = ("SELECT m.user_id, m.username, m.display_name, m.avatar_url, m.joined_at, m.left_at, m.roles, "
                  "rl.roblox_username, rl.roblox_id, s.rank AS staff_rank, s.status AS staff_status, s.department AS staff_department, "
                  "(SELECT COUNT(*) FROM mod_cases c WHERE c.guild_id = m.guild_id AND c.target_id = m.user_id AND c.status = 'active') AS case_count"
                  + base + " ORDER BY lower(COALESCE(m.display_name, m.username))")
        pg = await paginate_sql(self.db, select, "SELECT COUNT(*)" + base, params, page, per_page)
        for it in pg.items:
            it["roles"] = loads(it.get("roles"), [])
            it["user_id"] = str(it["user_id"])  # JS-safe snowflakes
            if it.get("roblox_id"):
                it["roblox_id"] = str(it["roblox_id"])
        return pg.as_dict()

    async def get(self, guild_id: int, user_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM members WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        if not r:
            return None
        d = dict(r)
        d["roles"] = loads(d.get("roles"), [])
        return d

    async def profile(self, guild_id: int, user_id: int, *, include_notes: bool = False, include_cases: bool = False) -> dict:
        member = await self.get(guild_id, user_id) or (self.p.actions.member(guild_id, user_id) and
                                                        {"user_id": user_id, **self.p.actions.member(guild_id, user_id)})
        if not member:
            raise ServiceError("That member is not in this server's directory.")
        out = {"member": member,
               "roblox": await self.p.verification.get_link(guild_id, user_id),
               "staff": await self.p.staff.get(guild_id, user_id),
               "officer": dict(r) if (r := await self.db.fetchone(
                   "SELECT o.name, o.roblox_username, u.callsign, d.short_code AS dept_code, d.name AS dept_name FROM officers o "
                   "LEFT JOIN units u ON u.id = o.unit_id LEFT JOIN departments d ON d.id = o.department_id "
                   "WHERE o.guild_id = ? AND o.discord_id = ? AND o.active = 1", (guild_id, user_id))) else None,
               "applications": row_dicts(await self.db.fetchall(
                   "SELECT id, number, form_name, status, created_at FROM applications WHERE guild_id = ? AND user_id = ? "
                   "ORDER BY id DESC LIMIT 20", (guild_id, user_id))),
               "shifts": await self.p.shifts.summary(guild_id, user_id)}
        if include_cases:
            out["cases"] = await self.p.moderation.history(guild_id, user_id)
        if include_notes:
            out["notes"] = await self.notes(guild_id, user_id)
        return out

    async def notes(self, guild_id: int, user_id: int) -> list[dict]:
        return row_dicts(await self.db.fetchall("SELECT * FROM member_notes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
                                                (guild_id, user_id)))

    async def add_note(self, guild_id: int, user_id: int, author_id: int, note: str) -> int:
        note = clean_text(note, 1000, required=True, field="Note")
        nid = await self.db.execute("INSERT INTO member_notes(guild_id, user_id, author_id, note) VALUES (?, ?, ?, ?)",
                                    (guild_id, user_id, author_id, note))
        await self.p.audit.record(guild_id, author_id, "member.note", str(user_id))
        return nid

    async def delete_note(self, guild_id: int, note_id: int, actor_id: int) -> bool:
        n = await self.db.execute_rowcount("DELETE FROM member_notes WHERE guild_id = ? AND id = ?", (guild_id, int(note_id)))
        if n:
            await self.p.audit.record(guild_id, actor_id, "member.note_delete", str(note_id))
        return bool(n)

    async def counts(self, guild_id: int) -> dict:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN rl.roblox_id IS NOT NULL THEN 1 ELSE 0 END) AS verified "
            "FROM members m LEFT JOIN roblox_links rl ON rl.guild_id = m.guild_id AND rl.discord_id = m.user_id "
            "WHERE m.guild_id = ? AND m.left_at IS NULL AND m.is_bot = 0", (guild_id,))
        return {"total": row["total"] or 0, "verified": row["verified"] or 0}
