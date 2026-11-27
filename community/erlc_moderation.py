"""In-game (ER:LC) moderation records, player profiles and moderation statistics (v5.0).

Records are kept against Roblox players. Staff can *optionally* carry the action out in-game through the
official ER:LC command endpoint (Virtual Server Management): the command result is stored with the record,
so a record never claims something happened in-game when it didn't.

    warning -> :pm <player> <message>       kick -> :kick <player> <reason>
    ban / tempban -> :ban <player>           unban -> :unban <player>       note -> (record only)

Temporary bans are lifted automatically (record expires; `:unban` is sent if the ban was sent in-game).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING, Any, Optional

from .common import (ServiceError, ValidationError, clean_text, fmt_duration, iso_in, iso_now, paginate_sql, parse_duration,
                     row_dicts, utcnow)

if TYPE_CHECKING:
    from .platform import CommunityPlatform

ACTIONS = {"warning": "Warning", "kick": "Kick", "ban": "Ban", "tempban": "Temporary ban", "unban": "Unban", "note": "Note"}
ACTION_ICON = {"warning": "⚠️", "kick": "👢", "ban": "🔨", "tempban": "⏳", "unban": "♻️", "note": "📝"}
ACTION_COLOUR = {"warning": 0xD29922, "kick": 0xF0883E, "ban": 0xDA3633, "tempban": 0xDA3633, "unban": 0x2EA043, "note": 0x6E7681}
PERMISSION = {"warning": "erlc.moderate", "kick": "erlc.moderate", "note": "erlc.moderate",
              "ban": "erlc.ban", "tempban": "erlc.ban", "unban": "erlc.ban"}
MAX_TEMPBAN = 365 * 86400
ROBLOX_NAME = re.compile(r"^[A-Za-z0-9_]{3,20}$")
_UNSAFE = re.compile(r"[\r\n\t]+")


def command_text(text: str, limit: int = 200) -> str:
    """One line, no control characters: ER:LC commands are a single line of text."""
    return _UNSAFE.sub(" ", str(text or "")).strip()[:limit]


class ErlcModerationService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db
        self.erlc = None  # object with async run_command(guild_id, command) and live (ErlcLive); set by the bot

    # ------------------------------------------------------------------ players
    async def resolve_player(self, guild_id: int, player: str) -> dict:
        """'Username' or Roblox ID -> {'roblox_id': int|None, 'username': str}. Never invents an ID."""
        text = str(player or "").strip().lstrip("@")
        if not text:
            raise ValidationError("Enter a Roblox username or user ID.")
        if text.isdigit():
            rid = int(text)
            row = await self.db.fetchone("SELECT username FROM erlc_players WHERE guild_id = ? AND roblox_id = ?", (guild_id, rid))
            if row:
                return {"roblox_id": rid, "username": row["username"]}
            user = await self._roblox_user(rid)
            if user is None:
                raise ServiceError("No Roblox account with that ID was found (or Roblox could not be reached).")
            return {"roblox_id": rid, "username": user.name}
        if not ROBLOX_NAME.match(text):
            raise ValidationError("Roblox usernames are 3-20 letters, numbers or underscores.")
        row = await self.db.fetchone("SELECT roblox_id, username FROM erlc_players WHERE guild_id = ? AND username = ? COLLATE NOCASE",
                                     (guild_id, text))
        if row:
            return {"roblox_id": int(row["roblox_id"]), "username": row["username"]}
        roblox = getattr(self.p.verification, "roblox", None)
        user = None
        if roblox is not None:
            try:
                user = await roblox.user_by_username(text)
            except Exception:
                user = None
        if user is not None:
            return {"roblox_id": int(user.id), "username": user.name}
        return {"roblox_id": None, "username": text}  # unknown to Roblox right now: kept by name only

    async def _roblox_user(self, rid: int):
        roblox = getattr(self.p.verification, "roblox", None)
        if roblox is None:
            return None
        try:
            return await roblox.user(rid)
        except Exception:
            return None

    async def _run(self, guild_id: int, command: str) -> tuple[str, str]:
        if self.erlc is None:
            return "failed", "ER:LC integration is not running."
        try:
            await self.erlc.run_command(guild_id, command)
            return "sent", f"Sent: {command}"
        except Exception as e:  # ERLCError has a friendly message
            msg = e.friendly() if hasattr(e, "friendly") else str(e)
            return "failed", msg[:300]

    # ------------------------------------------------------------------ records
    @staticmethod
    def _rec(r) -> dict:
        d = dict(r)
        for k in ("roblox_id", "moderator_id", "revoked_by"):
            d[k] = str(d[k]) if d.get(k) else None
        d["label"] = ACTIONS.get(d["action"], d["action"])
        return d

    async def get(self, guild_id: int, number: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM erlc_mod_records WHERE guild_id = ? AND number = ?", (guild_id, int(number)))
        return self._rec(r) if r else None

    async def list(self, guild_id: int, *, action: str = "", status: str = "", q: str = "", roblox_id: Optional[int] = None,
                   moderator_id: Optional[int] = None, since: str = "", page: int = 1, per_page: int = 25) -> dict:
        where, params = ["guild_id = ?"], [guild_id]
        if action in ACTIONS:
            where.append("action = ?")
            params.append(action)
        if status in {"active", "revoked", "expired"}:
            where.append("status = ?")
            params.append(status)
        if roblox_id:
            where.append("roblox_id = ?")
            params.append(int(roblox_id))
        if moderator_id:
            where.append("moderator_id = ?")
            params.append(int(moderator_id))
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if q:
            where.append("(roblox_username LIKE ? OR reason LIKE ? OR CAST(number AS TEXT) = ? OR CAST(roblox_id AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", q.lstrip("#"), q]
        base = " FROM erlc_mod_records WHERE " + " AND ".join(where)
        pg = await paginate_sql(self.db, "SELECT *" + base + " ORDER BY id DESC", "SELECT COUNT(*)" + base, params, page, per_page)
        pg.items = [self._rec(i) for i in pg.items]
        return pg.as_dict()

    async def create(self, guild_id: int, action: str, player: str, moderator_id: int, *, reason: str, evidence: str = "",
                     duration: Optional[str] = None, execute: bool = False, source: str = "dashboard") -> dict:
        if action not in ACTIONS:
            raise ValidationError("Unknown action.")
        reason = clean_text(reason, 500, required=action != "note", field="Reason") or "No reason given"
        evidence = clean_text(evidence, 500)
        target = await self.resolve_player(guild_id, player)
        seconds = parse_duration(duration or "", default_unit="h", max_seconds=MAX_TEMPBAN) if action == "tempban" else None
        if action == "unban" and not await self._active_bans(guild_id, target) and not execute:
            raise ServiceError(f"{target['username']} has no active ban on record. Tick 'carry out in-game' to unban them in ER:LC anyway.")

        in_game, detail = "not_requested", None
        if execute and action != "note":
            name = target["username"]
            if not ROBLOX_NAME.match(name):
                raise ValidationError("That username can't be used in an in-game command.")
            cmd = {"warning": f":pm {name} Warning from staff: {command_text(reason, 180)}",
                   "kick": f":kick {name} {command_text(reason, 180)}",
                   "ban": f":ban {name}", "tempban": f":ban {name}", "unban": f":unban {name}"}[action]
            in_game, detail = await self._run(guild_id, cmd)

        number = await self.db.next_number(guild_id, "erlc_record")
        await self.db.execute(
            "INSERT INTO erlc_mod_records(guild_id, number, action, roblox_id, roblox_username, moderator_id, reason, evidence, duration_s, "
            "expires_at, in_game, in_game_detail, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, action, target["roblox_id"], target["username"], int(moderator_id), reason, evidence or None, seconds,
             iso_in(seconds) if seconds else None, in_game, detail, source))
        if action == "unban":
            await self._close_bans(guild_id, target, f"Unbanned (record #{number})", moderator_id)
        await self.p.audit.record(guild_id, int(moderator_id), f"erlc.{action}", target["username"], {"record": number, "in_game": in_game})
        fields = [("Player", f"{target['username']}" + (f" ({target['roblox_id']})" if target["roblox_id"] else ""), True),
                  ("Record", f"#{number}", True)]
        if seconds:
            fields.append(("Duration", fmt_duration(seconds), True))
        fields.append(("Reason", reason, False))
        if evidence:
            fields.append(("Evidence", evidence, False))
        fields.append(("In-game", {"sent": "Carried out in-game", "failed": f"In-game command failed: {detail}",
                                   "not_requested": "Record only"}[in_game], False))
        await self.p.log(guild_id, "erlc_moderation", f"{ACTION_ICON[action]} In-game {ACTIONS[action].lower()}: {target['username']}",
                         fields=fields, actor_id=int(moderator_id), colour=ACTION_COLOUR[action])
        if action in {"ban", "tempban"}:
            await self.p.notifications.create(guild_id, "moderation", f"In-game ban: {target['username']}",
                                              body=f"Record #{number} by <@{moderator_id}> — {reason}",
                                              link=f"#/moderation/records/{number}", permission="erlc.view")
        await self.p.publish("erlc.record", guild_id, number=number, action=action, moderator_id=int(moderator_id))
        rec = await self.get(guild_id, number)
        if in_game == "failed":
            rec["warning"] = f"Record #{number} saved, but the in-game command failed: {detail}"
        return rec

    async def _active_bans(self, guild_id: int, target: dict) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM erlc_mod_records WHERE guild_id = ? AND action IN ('ban', 'tempban') AND status = 'active' AND "
            "(roblox_id = ? OR (roblox_id IS NULL AND roblox_username = ? COLLATE NOCASE))",
            (guild_id, target["roblox_id"] or -1, target["username"]))
        return row_dicts(rows)

    async def _close_bans(self, guild_id: int, target: dict, reason: str, actor_id: int) -> None:
        for b in await self._active_bans(guild_id, target):
            await self.db.execute("UPDATE erlc_mod_records SET status = 'revoked', revoked_by = ?, revoked_at = ?, revoke_reason = ?, "
                                  "updated_at = ? WHERE id = ?", (int(actor_id), iso_now(), reason[:300], iso_now(), b["id"]))

    async def revoke(self, guild_id: int, number: int, actor_id: int, reason: str = "", execute: bool = False) -> dict:
        rec = await self.get(guild_id, number)
        if not rec:
            raise ServiceError("Record not found.")
        if rec["status"] != "active":
            raise ServiceError(f"Record #{number} is already {rec['status']}.")
        if rec["action"] == "unban":
            raise ServiceError("An unban can't be revoked. Ban the player again instead.")
        detail = None
        if execute and rec["action"] in {"ban", "tempban"}:
            state, detail = await self._run(guild_id, f":unban {rec['roblox_username']}")
            if state == "failed":
                raise ServiceError(f"The in-game unban failed, so the record was left active: {detail}")
        await self.db.execute("UPDATE erlc_mod_records SET status = 'revoked', revoked_by = ?, revoked_at = ?, revoke_reason = ?, updated_at = ? "
                              "WHERE id = ?", (int(actor_id), iso_now(), clean_text(reason, 300) or None, iso_now(), self._id(rec)))
        await self.p.audit.record(guild_id, int(actor_id), "erlc.revoke", f"record:{number}", {"in_game": bool(detail)})
        await self.p.log(guild_id, "erlc_moderation", f"In-game record #{number} revoked",
                         fields=[("Player", rec["roblox_username"], True), ("Action", rec["label"], True)]
                         + ([("Reason", clean_text(reason, 300), False)] if reason else [])
                         + ([("In-game", detail, False)] if detail else []), actor_id=int(actor_id))
        await self.p.publish("erlc.record", guild_id, number=number, action="revoke")
        return await self.get(guild_id, number)

    @staticmethod
    def _id(rec: dict) -> int:
        return int(rec["id"])

    async def edit(self, guild_id: int, number: int, actor_id: int, *, reason: Optional[str] = None, evidence: Optional[str] = None) -> dict:
        rec = await self.get(guild_id, number)
        if not rec:
            raise ServiceError("Record not found.")
        new_reason = clean_text(reason, 500) if reason is not None else None
        if reason is not None and not new_reason:
            raise ValidationError("The reason can't be empty.")
        await self.db.execute("UPDATE erlc_mod_records SET reason = COALESCE(?, reason), evidence = COALESCE(?, evidence), updated_at = ? "
                              "WHERE id = ?", (new_reason, clean_text(evidence, 500) if evidence is not None else None, iso_now(), self._id(rec)))
        await self.p.audit.record(guild_id, int(actor_id), "erlc.edit", f"record:{number}")
        await self.p.publish("erlc.record", guild_id, number=number, action="edit")
        return await self.get(guild_id, number)

    async def expire_tempbans(self, guild_id: int) -> int:
        rows = await self.db.fetchall("SELECT * FROM erlc_mod_records WHERE guild_id = ? AND action = 'tempban' AND status = 'active' "
                                      "AND expires_at IS NOT NULL AND expires_at <= ?", (guild_id, iso_now()))
        for r in rows:
            detail = None
            if r["in_game"] == "sent":
                _state, detail = await self._run(guild_id, f":unban {r['roblox_username']}")
            await self.db.execute("UPDATE erlc_mod_records SET status = 'expired', revoke_reason = ?, updated_at = ? WHERE id = ?",
                                  (f"Expired{' — ' + detail if detail else ''}"[:300], iso_now(), r["id"]))
            await self.p.log(guild_id, "erlc_moderation", f"Temporary ban #{r['number']} expired: {r['roblox_username']}",
                             fields=[("In-game", detail or "Record only", False)])
        if rows:
            await self.p.publish("erlc.record", guild_id, action="expire")
        return len(rows)

    # ------------------------------------------------------------------ notes
    async def add_note(self, guild_id: int, player: str, author_id: int, note: str) -> dict:
        target = await self.resolve_player(guild_id, player)
        text = clean_text(note, 1000, required=True, field="Note")
        nid = await self.db.execute("INSERT INTO player_notes(guild_id, roblox_id, roblox_username, author_id, note) VALUES (?, ?, ?, ?, ?)",
                                    (guild_id, target["roblox_id"], target["username"], int(author_id), text))
        await self.p.audit.record(guild_id, int(author_id), "erlc.note", target["username"])
        return {"id": nid}

    async def delete_note(self, guild_id: int, note_id: int, actor_id: int) -> bool:
        n = await self.db.execute_rowcount("DELETE FROM player_notes WHERE guild_id = ? AND id = ?", (guild_id, int(note_id)))
        if n:
            await self.p.audit.record(guild_id, int(actor_id), "erlc.note_delete", f"note:{note_id}")
        return bool(n)

    # ------------------------------------------------------------------ profiles & lookup
    async def search_players(self, guild_id: int, q: str = "", *, online: Optional[bool] = None, page: int = 1, per_page: int = 25) -> dict:
        where, params = ["p.guild_id = ?"], [guild_id]
        if q:
            where.append("(p.username LIKE ? OR CAST(p.roblox_id AS TEXT) = ?)")
            params += [f"%{q.strip()}%", q.strip()]
        if online is not None:
            where.append("p.online = ?")
            params.append(1 if online else 0)
        base = " FROM erlc_players p WHERE " + " AND ".join(where)
        sel = ("SELECT p.*, (SELECT COUNT(*) FROM erlc_mod_records r WHERE r.guild_id = p.guild_id AND r.roblox_id = p.roblox_id "
               "AND r.action != 'note') AS records, (SELECT COUNT(*) FROM bolos b WHERE b.guild_id = p.guild_id AND b.status = 'active' "
               "AND (b.roblox_id = p.roblox_id OR LOWER(b.roblox_username) = LOWER(p.username))) AS active_bolos")
        pg = await paginate_sql(self.db, sel + base + " ORDER BY p.online DESC, p.last_seen DESC", "SELECT COUNT(*)" + base, params, page, per_page)
        for it in pg.items:
            it["roblox_id"] = str(it["roblox_id"])
        return pg.as_dict()

    async def profile(self, guild_id: int, key: str, *, include_discord_cases: bool = False, include_notes: bool = True) -> dict:
        key = str(key or "").strip()
        if key.isdigit():
            rid, row = int(key), await self.db.fetchone("SELECT * FROM erlc_players WHERE guild_id = ? AND roblox_id = ?", (guild_id, int(key)))
            name = row["username"] if row else None
        else:
            row = await self.db.fetchone("SELECT * FROM erlc_players WHERE guild_id = ? AND username = ? COLLATE NOCASE", (guild_id, key))
            rid, name = (int(row["roblox_id"]), row["username"]) if row else (None, key)
        if rid is None and name is None:
            raise ServiceError("Player not found.")
        if name is None:
            rec = await self.db.fetchone("SELECT roblox_username FROM erlc_mod_records WHERE guild_id = ? AND roblox_id = ? LIMIT 1", (guild_id, rid))
            user = None if rec else await self._roblox_user(rid)
            name = rec["roblox_username"] if rec else (user.name if user else f"Roblox {rid}")
        match = "(roblox_id = ? OR (roblox_id IS NULL AND roblox_username = ? COLLATE NOCASE))"
        mparams = (rid or -1, name)
        records = [self._rec(r) for r in await self.db.fetchall(
            f"SELECT * FROM erlc_mod_records WHERE guild_id = ? AND {match} ORDER BY id DESC LIMIT 100", (guild_id, *mparams))]
        notes = row_dicts(await self.db.fetchall(f"SELECT * FROM player_notes WHERE guild_id = ? AND {match} ORDER BY id DESC LIMIT 50",
                                                 (guild_id, *mparams))) if include_notes else None
        for n in notes or []:
            n["author_id"] = str(n["author_id"])
            n["roblox_id"] = str(n["roblox_id"]) if n["roblox_id"] else None
        bolos = row_dicts(await self.db.fetchall(
            "SELECT number, subject, priority, status, created_at FROM bolos WHERE guild_id = ? AND "
            "(roblox_id = ? OR LOWER(roblox_username) = LOWER(?)) ORDER BY id DESC LIMIT 20", (guild_id, rid or -1, name)))
        events = row_dicts(await self.db.fetchall(
            "SELECT kind, at, team FROM erlc_player_events WHERE guild_id = ? AND roblox_id = ? ORDER BY id DESC LIMIT 30",
            (guild_id, rid or -1))) if rid else []
        link = await self.db.fetchone("SELECT discord_id FROM roblox_links WHERE guild_id = ? AND roblox_id = ?", (guild_id, rid)) if rid else None
        discord_id = str(link["discord_id"]) if link else None
        discord_cases = None
        if discord_id and include_discord_cases:
            discord_cases = [self.p.moderation._case(r) for r in await self.db.fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? ORDER BY id DESC LIMIT 25", (guild_id, int(discord_id)))]
        live = None
        if self.erlc is not None and getattr(self.erlc, "live", None) is not None and rid:
            live = next((p for p in self.erlc.live.state(guild_id).players if p["roblox_id"] == str(rid)), None)
        counts: dict[str, int] = {}
        for r in records:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        return {"player": {"roblox_id": str(rid) if rid else None, "username": name,
                           "first_seen": row["first_seen"] if row else None, "last_seen": row["last_seen"] if row else None,
                           "sessions": row["sessions"] if row else 0, "online": bool(live) or bool(row and row["online"]),
                           "team": (live or {}).get("team") or (row["team"] if row else None),
                           "permission": (live or {}).get("permission") or (row["permission"] if row else None),
                           "known": bool(row)},
                "live": live, "discord_id": discord_id, "records": records, "counts": counts,
                "active_ban": any(r["action"] in {"ban", "tempban"} and r["status"] == "active" for r in records),
                "notes": notes, "bolos": bolos, "events": events, "discord_cases": discord_cases}

    # ------------------------------------------------------------------ statistics
    async def stats(self, guild_id: int, days: int = 30) -> dict:
        days = max(1, min(365, int(days or 30)))
        since = (utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        game = {r["action"]: r["n"] for r in await self.db.fetchall(
            "SELECT action, COUNT(*) AS n FROM erlc_mod_records WHERE guild_id = ? AND created_at >= ? GROUP BY action", (guild_id, since))}
        disc = {r["action"]: r["n"] for r in await self.db.fetchall(
            "SELECT action, COUNT(*) AS n FROM mod_cases WHERE guild_id = ? AND created_at >= ? GROUP BY action", (guild_id, since))}
        span = min(days, 30)
        start = (utcnow() - dt.timedelta(days=span - 1)).date()
        labels = [(start + dt.timedelta(days=i)).isoformat() for i in range(span)]
        series = {"in_game": dict.fromkeys(labels, 0), "discord": dict.fromkeys(labels, 0)}
        for table, key in (("erlc_mod_records", "in_game"), ("mod_cases", "discord")):
            for r in await self.db.fetchall(f"SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM {table} WHERE guild_id = ? AND "
                                            f"created_at >= ? AND action != 'note' GROUP BY d", (guild_id, labels[0])):
                if r["d"] in series[key]:
                    series[key][r["d"]] = r["n"]
        active = {
            "in_game_bans": await self.db.fetchval("SELECT COUNT(*) FROM erlc_mod_records WHERE guild_id = ? AND action IN ('ban','tempban') "
                                                   "AND status = 'active'", (guild_id,), 0),
            "discord_bans": await self.db.fetchval("SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND action = 'ban' AND status = 'active'",
                                                   (guild_id,), 0),
            "discord_timeouts": await self.db.fetchval("SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND action = 'timeout' AND "
                                                       "status = 'active' AND (expires_at IS NULL OR expires_at > ?)", (guild_id, iso_now()), 0),
            "open_reports": await self.db.fetchval("SELECT COUNT(*) FROM member_reports WHERE guild_id = ? AND status = 'open'", (guild_id,), 0),
        }
        return {"days": days, "in_game": game, "discord": disc, "labels": labels,
                "series": {k: list(v.values()) for k, v in series.items()}, "active": active,
                "moderators": (await self.staff_records(guild_id, days))[:8]}

    async def staff_records(self, guild_id: int, days: int = 30) -> list[dict]:
        since = (utcnow() - dt.timedelta(days=max(1, min(365, int(days or 30))))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        people: dict[str, dict[str, Any]] = {}
        for table, key in (("erlc_mod_records", "in_game"), ("mod_cases", "discord")):
            for r in await self.db.fetchall(f"SELECT moderator_id, action, COUNT(*) AS n FROM {table} WHERE guild_id = ? AND created_at >= ? "
                                            f"GROUP BY moderator_id, action", (guild_id, since)):
                if not r["moderator_id"]:
                    continue  # automatic actions
                p = people.setdefault(str(r["moderator_id"]), {"user_id": str(r["moderator_id"]), "in_game": {}, "discord": {}, "total": 0})
                p[key][r["action"]] = r["n"]
                p["total"] += r["n"]
        if people:
            marks = ",".join("?" * len(people))
            for m in await self.db.fetchall(f"SELECT user_id, username, display_name, avatar_url FROM members WHERE guild_id = ? AND user_id IN ({marks})",
                                            (guild_id, *[int(k) for k in people])):
                people[str(m["user_id"])].update(name=m["display_name"] or m["username"], avatar=m["avatar_url"])
        return sorted(people.values(), key=lambda p: -p["total"])

    async def active_punishments(self, guild_id: int) -> dict:
        game = [self._rec(r) for r in await self.db.fetchall(
            "SELECT * FROM erlc_mod_records WHERE guild_id = ? AND action IN ('ban','tempban') AND status = 'active' ORDER BY id DESC LIMIT 200",
            (guild_id,))]
        disc = [self.p.moderation._case(r) for r in await self.db.fetchall(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND status = 'active' AND (action = 'ban' OR (action = 'timeout' AND "
            "(expires_at IS NULL OR expires_at > ?))) ORDER BY id DESC LIMIT 200", (guild_id, iso_now()))]
        return {"in_game": game, "discord": disc}
