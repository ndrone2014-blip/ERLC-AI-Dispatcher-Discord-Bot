"""Live ER:LC server state (v5.0): players, joins/leaves, in-game staff and the documented log sections.

The sync loop already polls GET /v2/server; v5.0 asks the same request for the documented extra sections
(Staff, JoinLogs, Queue, ModCalls, CommandLogs, KillLogs) and hands the snapshot to `ErlcLive.process`.

Rules:
  * Nothing is invented. A section the API did not return is reported as unavailable, not as empty.
  * Joins and leaves are detected by comparing the real player list between two polls. The first poll after
    start-up is only a baseline, so a restart never reports everyone as "joined".
  * If the API refuses the extra sections, they are switched off for 15 minutes and 911 polling carries on.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from core import activity
from database.models import iso_now

from .models import ServerSnapshot

log = logging.getLogger(__name__)

EXTRA_FLAGS = ("Staff", "JoinLogs", "Queue", "ModCalls", "CommandLogs", "KillLogs")
EXTRA_RETRY_S = 900
# The API's per-player `Permission` value; anything other than "Normal" is in-game staff.
STAFF_ORDER = ["Server Owner", "Server Co-Owner", "Server Administrator", "Server Moderator", "Server Helper"]
LOG_LIMIT = 100

Notifier = Callable[..., Awaitable[Any]]


def split_player(raw: Any) -> tuple[str, Optional[int]]:
    """Documented format "PlayerName:Id"."""
    s = str(raw or "")
    name, _, rid = s.rpartition(":")
    if not name:
        return s, None
    try:
        return name, int(rid)
    except ValueError:
        return s, None


def _ts(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class LiveState:
    guild_id: int
    players: list[dict] = field(default_factory=list)
    online: dict[int, dict] = field(default_factory=dict)   # roblox_id -> player
    baseline: bool = False
    updated_at: Optional[str] = None
    updated_mono: float = 0.0
    server_name: Optional[str] = None
    current_players: Optional[int] = None
    max_players: Optional[int] = None
    join_key: Optional[str] = None
    offline: bool = False                     # API said the private server is offline (no players)
    sections: dict[str, Any] = field(default_factory=dict)  # present sections only
    extras_disabled_until: float = 0.0
    extras_error: Optional[str] = None


class ErlcLive:
    def __init__(self, db, bus, *, notify: Optional[Notifier] = None, enabled_check: Optional[Callable[[int], Awaitable[bool]]] = None):
        self.db = db
        self.bus = bus
        self.notify = notify                 # NotificationService.create (set by the bot)
        self.enabled_check = enabled_check   # per-guild toggle for the extra sections
        self._states: dict[int, LiveState] = {}

    def state(self, guild_id: int) -> LiveState:
        st = self._states.get(guild_id)
        if st is None:
            st = self._states[guild_id] = LiveState(guild_id)
        return st

    # ------------------------------------------------------------------ extra sections
    async def extra_flags(self, guild_id: int) -> dict[str, bool]:
        st = self.state(guild_id)
        if time.time() < st.extras_disabled_until:
            return {}
        if self.enabled_check is not None and not await self.enabled_check(guild_id):
            return {}
        return {f: True for f in EXTRA_FLAGS}

    def extras_failed(self, guild_id: int, reason: str) -> None:
        st = self.state(guild_id)
        st.extras_disabled_until = time.time() + EXTRA_RETRY_S
        st.extras_error = reason[:200]
        log.warning("ER:LC extra sections refused for guild %s (%s); retrying in %s min", guild_id, reason, EXTRA_RETRY_S // 60)

    # ------------------------------------------------------------------ processing
    @staticmethod
    def _player(p) -> dict:
        loc = p.location
        perm = p.permission or "Normal"
        return {"name": p.name, "roblox_id": str(p.roblox_id) if p.roblox_id else None, "team": p.team, "callsign": p.callsign,
                "permission": perm, "staff": perm != "Normal", "wanted_stars": p.wanted_stars,
                "location": ({"postal": loc.postal, "street": loc.street} if loc and (loc.postal or loc.street) else None)}

    def _sections(self, raw: dict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(raw.get("JoinLogs"), list):
            items = []
            for e in raw["JoinLogs"][-LOG_LIMIT:]:
                if isinstance(e, dict):
                    name, rid = split_player(e.get("Player"))
                    items.append({"player": name, "roblox_id": str(rid) if rid else None, "join": bool(e.get("Join")), "ts": _ts(e.get("Timestamp"))})
            out["join_logs"] = items[::-1]
        if isinstance(raw.get("KillLogs"), list):
            items = []
            for e in raw["KillLogs"][-LOG_LIMIT:]:
                if isinstance(e, dict):
                    k, kid = split_player(e.get("Killer"))
                    v, vid = split_player(e.get("Killed"))
                    items.append({"killer": k, "killer_id": str(kid) if kid else None, "killed": v, "killed_id": str(vid) if vid else None,
                                  "ts": _ts(e.get("Timestamp"))})
            out["kill_logs"] = items[::-1]
        if isinstance(raw.get("CommandLogs"), list):
            items = []
            for e in raw["CommandLogs"][-LOG_LIMIT:]:
                if isinstance(e, dict):
                    name, rid = split_player(e.get("Player"))
                    items.append({"player": name, "roblox_id": str(rid) if rid else None, "command": str(e.get("Command") or "")[:300],
                                  "ts": _ts(e.get("Timestamp"))})
            out["command_logs"] = items[::-1]
        if isinstance(raw.get("ModCalls"), list):
            items = []
            for e in raw["ModCalls"][-LOG_LIMIT:]:
                if isinstance(e, dict):
                    c, cid = split_player(e.get("Caller"))
                    m, mid = split_player(e.get("Moderator")) if e.get("Moderator") else (None, None)
                    items.append({"caller": c, "caller_id": str(cid) if cid else None, "moderator": m,
                                  "moderator_id": str(mid) if mid else None, "ts": _ts(e.get("Timestamp"))})
            out["mod_calls"] = items[::-1]
        if isinstance(raw.get("Queue"), list):
            out["queue"] = [str(x) for x in raw["Queue"] if isinstance(x, (int, str))]
        staff = raw.get("Staff")
        if isinstance(staff, dict):
            members = []
            for group, val in staff.items():
                if isinstance(val, dict):
                    members += [{"roblox_id": str(k), "name": str(v), "group": str(group)} for k, v in val.items()]
                elif isinstance(val, list):
                    members += [{"roblox_id": str(x), "name": None, "group": str(group)} for x in val]
            out["staff"] = members
        return out

    async def process(self, guild_id: int, snap: ServerSnapshot) -> dict:
        st = self.state(guild_id)
        now = iso_now()
        players = [self._player(p) for p in snap.players]
        current = {int(p["roblox_id"]): p for p in players if p["roblox_id"]}
        st.players = sorted(players, key=lambda p: (not p["staff"], (p["team"] or "~"), p["name"].lower()))
        st.server_name, st.current_players, st.max_players, st.join_key = snap.name, snap.current_players, snap.max_players, snap.join_key
        st.updated_at, st.updated_mono, st.offline = now, time.monotonic(), False
        st.sections = self._sections(snap.raw or {})
        return await self._apply(st, current, now)

    async def server_offline(self, guild_id: int) -> dict:
        """The API reported the private server offline (error 3002): everyone who was online has left."""
        st = self.state(guild_id)
        st.offline, st.players, st.current_players = True, [], 0
        st.updated_at, st.updated_mono = iso_now(), time.monotonic()
        return await self._apply(st, {}, st.updated_at)

    async def _apply(self, st: LiveState, current: dict[int, dict], now: str) -> dict:
        gid = st.guild_id
        for rid, p in current.items():
            await self.db.execute(
                "INSERT INTO erlc_players(guild_id, roblox_id, username, last_seen, online, team, permission, callsign) VALUES (?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(guild_id, roblox_id) DO UPDATE SET username = excluded.username, last_seen = excluded.last_seen, online = 1, "
                "team = excluded.team, permission = excluded.permission, callsign = excluded.callsign",
                (gid, rid, p["name"], now, p["team"], p["permission"], p["callsign"]))
        if not st.baseline:
            # First successful poll since start-up: remember who is online, report nothing.
            st.baseline, st.online = True, current
            marks = ",".join("?" * len(current)) or "NULL"
            await self.db.execute(f"UPDATE erlc_players SET online = 0 WHERE guild_id = ? AND online = 1 AND roblox_id NOT IN ({marks})",
                                  (gid, *current.keys()))
            return {"joins": [], "leaves": [], "baseline": True}
        joins = [current[r] for r in current.keys() - st.online.keys()]
        leaves = [st.online[r] for r in st.online.keys() - current.keys()]
        st.online = current
        for p in joins:
            await self.db.execute("INSERT INTO erlc_player_events(guild_id, roblox_id, username, kind, at, team) VALUES (?, ?, ?, 'join', ?, ?)",
                                  (gid, int(p["roblox_id"]), p["name"], now, p["team"]))
            await self.db.execute("UPDATE erlc_players SET last_join = ?, sessions = sessions + 1 WHERE guild_id = ? AND roblox_id = ?",
                                  (now, gid, int(p["roblox_id"])))
        for p in leaves:
            await self.db.execute("INSERT INTO erlc_player_events(guild_id, roblox_id, username, kind, at, team) VALUES (?, ?, ?, 'leave', ?, ?)",
                                  (gid, int(p["roblox_id"]), p["name"], now, p["team"]))
            await self.db.execute("UPDATE erlc_players SET last_leave = ?, online = 0 WHERE guild_id = ? AND roblox_id = ?",
                                  (now, gid, int(p["roblox_id"])))
        if joins or leaves:
            await self._record_activity(gid, joins, leaves)
            if joins:
                await self._bolo_matches(gid, joins)
            await self.bus.publish("erlc.players", gid, joins=len(joins), leaves=len(leaves))
        return {"joins": joins, "leaves": leaves, "baseline": False}

    async def _record_activity(self, gid: int, joins: list[dict], leaves: list[dict]) -> None:
        for kind, group in (("joined", joins), ("left", leaves)):
            if not group:
                continue
            if len(group) <= 5:
                for p in group:
                    await activity.record(self.db, gid, "erlc_players", f"{p['name']} {kind} the server",
                                          body=f"Team: {p['team']}" if p.get("team") else "", link=f"moderation/player/{p['roblox_id']}")
            else:
                names = ", ".join(p["name"] for p in group[:12]) + ("…" if len(group) > 12 else "")
                await activity.record(self.db, gid, "erlc_players", f"{len(group)} players {kind} the server", body=names, link="erlc/players")

    async def _bolo_matches(self, gid: int, joins: list[dict]) -> None:
        """A player with an active BOLO joined: tell staff. A BOLO is information, never an automatic ban."""
        ids = [int(p["roblox_id"]) for p in joins]
        names = [p["name"].lower() for p in joins]
        rows = await self.db.fetchall(
            f"SELECT number, subject, priority, roblox_id, roblox_username FROM bolos WHERE guild_id = ? AND status = 'active' AND "
            f"(roblox_id IN ({','.join('?' * len(ids))}) OR LOWER(roblox_username) IN ({','.join('?' * len(names))}))",
            (gid, *ids, *names))
        for r in rows:
            who = r["roblox_username"] or r["subject"]
            await activity.record(self.db, gid, "bolos", f"Player with BOLO #{r['number']} joined: {who}",
                                  body=r["subject"], link=f"bolos/{r['number']}")
            if self.notify is not None:
                try:
                    await self.notify(gid, "dispatch", f"BOLO #{r['number']} player in-game: {who}",
                                      body=f"{r['subject']} — this is a BOLO, not a ban. Review it before acting.",
                                      severity="critical" if r["priority"] in {"high", "critical"} else "warning",
                                      link=f"#/bolos/{r['number']}", permission="bolo.view",
                                      dedupe_key=f"bolo-join:{r['number']}:{who.lower()}", window_seconds=1800)
                except Exception:
                    log.exception("Could not create BOLO join notification")

    # ------------------------------------------------------------------ read model for the dashboard
    def snapshot(self, guild_id: int) -> dict:
        st = self.state(guild_id)
        age = round(time.monotonic() - st.updated_mono) if st.updated_mono else None
        staff_online = [p for p in st.players if p["staff"]]
        teams: dict[str, int] = {}
        for p in st.players:
            teams[p["team"] or "Unknown"] = teams.get(p["team"] or "Unknown", 0) + 1
        return {"updated_at": st.updated_at, "age_s": age, "offline": st.offline, "server": st.server_name,
                "current_players": st.current_players, "max_players": st.max_players, "join_key": st.join_key,
                "players": st.players, "staff_online": staff_online, "teams": teams,
                "sections": {k: k in st.sections for k in ("join_logs", "kill_logs", "command_logs", "mod_calls", "queue", "staff")},
                "extras_error": st.extras_error if time.time() < st.extras_disabled_until else None}

    def section(self, guild_id: int, name: str) -> Optional[list]:
        return self.state(guild_id).sections.get(name)
