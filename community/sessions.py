"""Community sessions (roleplay sessions, trainings, events): scheduling, announcements,
reminders and participants."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Optional

from .common import ServiceError, ValidationError, clean_text, iso_now, parse_iso, row_dicts, utcnow

if TYPE_CHECKING:
    from .platform import CommunityPlatform

DEFAULTS = {"announce_channel_id": None, "ping_role_id": None, "default_reminder_minutes": 15}


def _ts(iso: Optional[str], style: str = "F") -> str:
    d = parse_iso(iso)
    return f"<t:{int(d.timestamp())}:{style}>" if d else "—"


class SessionService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "session_settings", DEFAULTS)

    async def update_settings(self, guild_id: int, changes: dict, actor_id: int) -> dict:
        clean = {}
        for k in ("announce_channel_id", "ping_role_id"):
            if k in changes:
                v = changes[k]
                clean[k] = int(v) if v not in (None, "", 0, "0") else None
        if "default_reminder_minutes" in changes:
            clean["default_reminder_minutes"] = max(0, min(1440, int(changes["default_reminder_minutes"] or 0)))
        return await self.p.update_settings(guild_id, "session_settings", DEFAULTS, clean, actor_id)

    async def get(self, guild_id: int, number: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM community_sessions WHERE guild_id = ? AND number = ?", (guild_id, int(number)))
        if not r:
            return None
        d = dict(r)
        d["participants"] = [str(x["user_id"]) for x in await self.db.fetchall(
            "SELECT user_id FROM session_participants WHERE session_id = ? ORDER BY joined_at", (d["id"],))]
        d["participant_count"] = len(d["participants"])
        d["host_id"] = str(d["host_id"]) if d.get("host_id") else None
        return d

    async def list(self, guild_id: int, status: str = "upcoming", limit: int = 50) -> list[dict]:
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM session_participants p WHERE p.session_id = s.id) AS participant_count "
               "FROM community_sessions s WHERE s.guild_id = ?")
        params: list = [guild_id]
        if status == "upcoming":
            sql += " AND s.status IN ('scheduled','active') ORDER BY COALESCE(s.started_at, s.scheduled_for)"
        elif status in {"scheduled", "active", "ended", "cancelled"}:
            sql += " AND s.status = ? ORDER BY s.id DESC"
            params.append(status)
        else:
            sql += " ORDER BY s.id DESC"
        rows = row_dicts(await self.db.fetchall(sql + " LIMIT ?", params + [limit]))
        for r in rows:
            r["host_id"] = str(r["host_id"]) if r.get("host_id") else None
        return rows

    def _embed(self, s: dict, headline: str, colour: int) -> dict:
        fields = [{"name": "Host", "value": f"<@{s['host_id']}>" if s.get("host_id") else "—", "inline": True},
                  {"name": "When", "value": _ts(s.get("started_at") or s.get("scheduled_for")), "inline": True},
                  {"name": "Status", "value": s["status"].title(), "inline": True}]
        if s.get("department"):
            fields.append({"name": "Department", "value": s["department"], "inline": True})
        out = {"title": f"{headline} · {s['name']}", "description": s.get("description") or None, "color": colour,
               "fields": fields, "footer": {"text": f"Session #{s['number']} • Hive Dispatch | ERLC"}, "timestamp": iso_now()}
        if s["status"] in {"scheduled", "active"}:
            out["_session"] = s["number"]  # the Discord layer adds Join / Leave buttons
        return out

    async def _announce(self, s: dict, headline: str, colour: int, *, ping: bool) -> None:
        cfg = await self.settings(s["guild_id"])
        channel = s.get("channel_id") or cfg.get("announce_channel_id")
        if not channel:
            return
        mention = f"<@&{cfg['ping_role_id']}>" if ping and cfg.get("ping_role_id") else None
        msg_id, err = await self.p.actions.send(s["guild_id"], int(channel), content=mention, embed=self._embed(s, headline, colour),
                                                mention_roles=[int(cfg["ping_role_id"])] if mention else None)
        if msg_id and not s.get("message_id"):
            await self.db.execute("UPDATE community_sessions SET message_id = ?, channel_id = ? WHERE id = ?", (msg_id, int(channel), s["id"]))
        if err:
            await self.p.log(s["guild_id"], "sessions", "Session announcement failed", description=err)

    async def create(self, guild_id: int, actor_id: int, *, name: str, description: str = "", scheduled_for: Optional[str] = None,
                     host_id: Optional[int] = None, department: str = "", reminder_minutes: Optional[int] = None,
                     channel_id: Optional[int] = None, announce: bool = True) -> dict:
        name = clean_text(name, 80, required=True, field="Session name")
        when = None
        if scheduled_for:
            d = parse_iso(scheduled_for if ("T" in scheduled_for) else scheduled_for.replace(" ", "T"))
            if d is None:
                raise ValidationError("Use a date and time like 2026-12-24T19:00 (UTC) for the start time.")
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            if d < utcnow() - dt.timedelta(minutes=5):
                raise ValidationError("The start time is in the past.")
            when = d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cfg = await self.settings(guild_id)
        number = await self.db.next_number(guild_id, "session")
        sid = await self.db.execute(
            "INSERT INTO community_sessions(guild_id, number, name, description, host_id, department, scheduled_for, reminder_minutes, "
            "channel_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, name, clean_text(description, 1500) or None, int(host_id or actor_id), clean_text(department, 40) or None,
             when, int(cfg["default_reminder_minutes"] if reminder_minutes is None else reminder_minutes), channel_id, actor_id))
        s = dict(await self.db.fetchone("SELECT * FROM community_sessions WHERE id = ?", (sid,)))
        if announce:
            await self._announce(s, "📅 Session Scheduled" if when else "📅 New Session", 0x3CC8F0, ping=False)
        await self.p.audit.record(guild_id, actor_id, "session.create", f"session:{number}", {"name": name})
        await self.p.log(guild_id, "sessions", f"Session #{number} Created", fields=[("Name", name, True), ("Start", _ts(when), True)],
                         actor_id=actor_id)
        await self.p.publish("session.created", guild_id, session_number=number, session_name=name, actor_id=actor_id)
        return await self.get(guild_id, number)

    async def start(self, guild_id: int, number: int, actor_id: int) -> dict:
        s = await self.get(guild_id, number)
        if not s:
            raise ServiceError("Session not found.")
        if s["status"] != "scheduled":
            raise ServiceError(f"That session is {s['status']}.")
        await self.db.execute("UPDATE community_sessions SET status = 'active', started_at = ? WHERE id = ?", (iso_now(), s["id"]))
        s = await self.get(guild_id, number)
        await self._announce(s, "🟢 Session Started", 0x2EA043, ping=True)
        await self.p.log(guild_id, "sessions", f"Session #{number} Started", fields=[("Name", s["name"], True)], actor_id=actor_id)
        await self.p.publish("session.started", guild_id, session_number=number, session_name=s["name"], actor_id=actor_id)
        return s

    async def end(self, guild_id: int, number: int, actor_id: int, notes: str = "") -> dict:
        s = await self.get(guild_id, number)
        if not s:
            raise ServiceError("Session not found.")
        if s["status"] not in {"active", "scheduled"}:
            raise ServiceError(f"That session is already {s['status']}.")
        status = "ended" if s["status"] == "active" else "cancelled"
        await self.db.execute("UPDATE community_sessions SET status = ?, ended_at = ?, notes = COALESCE(?, notes) WHERE id = ?",
                              (status, iso_now(), clean_text(notes, 1000) or None, s["id"]))
        s = await self.get(guild_id, number)
        await self._announce(s, "🔴 Session Ended" if status == "ended" else "⚪ Session Cancelled", 0x6E7681, ping=False)
        await self.p.log(guild_id, "sessions", f"Session #{number} {status.title()}",
                         fields=[("Name", s["name"], True), ("Participants", str(s["participant_count"]), True)], actor_id=actor_id)
        await self.p.publish(f"session.{status}", guild_id, session_number=number, session_name=s["name"], actor_id=actor_id)
        return s

    async def join(self, guild_id: int, number: int, user_id: int) -> dict:
        s = await self.get(guild_id, number)
        if not s or s["status"] not in {"scheduled", "active"}:
            raise ServiceError("That session is not open.")
        await self.db.execute("INSERT OR IGNORE INTO session_participants(session_id, user_id) VALUES (?, ?)", (s["id"], int(user_id)))
        return await self.get(guild_id, number)

    async def leave(self, guild_id: int, number: int, user_id: int) -> dict:
        s = await self.get(guild_id, number)
        if not s:
            raise ServiceError("Session not found.")
        await self.db.execute("DELETE FROM session_participants WHERE session_id = ? AND user_id = ?", (s["id"], int(user_id)))
        return await self.get(guild_id, number)

    async def tick(self, guild_id: int) -> int:
        """Send due reminders. Returns how many were sent."""
        rows = await self.db.fetchall("SELECT * FROM community_sessions WHERE guild_id = ? AND status = 'scheduled' AND reminded = 0 "
                                      "AND scheduled_for IS NOT NULL AND reminder_minutes > 0", (guild_id,))
        sent = 0
        now = utcnow()
        for r in rows:
            start = parse_iso(r["scheduled_for"])
            if start and start - dt.timedelta(minutes=r["reminder_minutes"]) <= now <= start + dt.timedelta(minutes=30):
                await self.db.execute("UPDATE community_sessions SET reminded = 1 WHERE id = ?", (r["id"],))
                s = await self.get(guild_id, r["number"])
                await self._announce(s, "⏰ Session Starting Soon", 0xD29922, ping=True)
                for uid in s["participants"]:
                    await self.p.actions.dm(int(uid), content=f"⏰ **{s['name']}** starts {_ts(s['scheduled_for'], 'R')}.")
                await self.p.publish("session.reminder", guild_id, session_number=s["number"], session_name=s["name"])
                sent += 1
        return sent
