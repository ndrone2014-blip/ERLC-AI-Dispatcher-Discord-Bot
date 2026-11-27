"""Analytics built only from recorded data (no estimates, no padding).

Each metric is a daily series over the chosen range; days with no records are real zeros
because the underlying events are recorded as they happen. Data that the bot has never
been able to record (e.g. member joins before v4.0 was installed) is simply absent and the
dashboard says so via `tracking_since`.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import TYPE_CHECKING, Optional

from .common import ValidationError, utcnow

if TYPE_CHECKING:
    from .platform import CommunityPlatform

RANGES = {"today": 0, "yesterday": 1, "7d": 7, "30d": 30, "90d": 90}

# metric -> (label, SQL returning (day, value) rows; params: guild, start, end)
METRICS: dict[str, tuple[str, str]] = {
    "member_joins": ("Member joins", "SELECT substr(at,1,10) d, COUNT(*) v FROM member_events WHERE guild_id=? AND kind='join' AND at>=? AND at<? GROUP BY d"),
    "member_leaves": ("Member leaves", "SELECT substr(at,1,10) d, COUNT(*) v FROM member_events WHERE guild_id=? AND kind='leave' AND at>=? AND at<? GROUP BY d"),
    "verifications": ("Verifications", "SELECT substr(at,1,10) d, COUNT(*) v FROM verification_history WHERE guild_id=? AND action IN ('verified','manual_link') AND at>=? AND at<? GROUP BY d"),
    "applications": ("Applications submitted", "SELECT substr(created_at,1,10) d, COUNT(*) v FROM applications WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY d"),
    "moderation": ("Moderation actions", "SELECT substr(created_at,1,10) d, COUNT(*) v FROM mod_cases WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY d"),
    "shift_hours": ("Shift hours", "SELECT substr(started_at,1,10) d, ROUND(SUM(duration_s)/3600.0, 1) v FROM shifts WHERE guild_id=? AND ended_at IS NOT NULL AND started_at>=? AND started_at<? GROUP BY d"),
    "incidents": ("Dispatch incidents", "SELECT substr(created_at,1,10) d, COUNT(*) v FROM incidents WHERE guild_id=? AND verification!='test' AND created_at>=? AND created_at<? GROUP BY d"),
    "sessions": ("Sessions held", "SELECT substr(COALESCE(started_at, created_at),1,10) d, COUNT(*) v FROM community_sessions WHERE guild_id=? AND status IN ('active','ended') AND COALESCE(started_at, created_at)>=? AND COALESCE(started_at, created_at)<? GROUP BY d"),
    "session_participants": ("Session participants", "SELECT substr(p.joined_at,1,10) d, COUNT(*) v FROM session_participants p JOIN community_sessions s ON s.id=p.session_id WHERE s.guild_id=? AND p.joined_at>=? AND p.joined_at<? GROUP BY d"),
    "commands": ("Commands used", "SELECT substr(at,1,10) d, COUNT(*) v FROM command_usage WHERE guild_id=? AND at>=? AND at<? GROUP BY d"),
    "ai_requests": ("AI requests", "SELECT substr(at,1,10) d, COUNT(*) v FROM ai_usage WHERE guild_id=? AND at>=? AND at<? GROUP BY d"),
    "automation_runs": ("Automation runs", "SELECT substr(at,1,10) d, COUNT(*) v FROM automation_runs WHERE guild_id=? AND at>=? AND at<? GROUP BY d"),
}
TRACKING_SINCE = {
    "member_joins": "SELECT MIN(at) FROM member_events WHERE guild_id=?",
    "member_leaves": "SELECT MIN(at) FROM member_events WHERE guild_id=?",
    "commands": "SELECT MIN(at) FROM command_usage WHERE guild_id=?",
}


def resolve_range(range_key: str = "7d", start: Optional[str] = None, end: Optional[str] = None) -> tuple[dt.date, dt.date]:
    """Returns [first_day, last_day] inclusive (UTC dates)."""
    today = utcnow().date()
    if range_key == "custom":
        try:
            a, b = dt.date.fromisoformat(start or ""), dt.date.fromisoformat(end or "")
        except ValueError:
            raise ValidationError("Custom ranges need a start and end date (YYYY-MM-DD).") from None
        if b < a:
            a, b = b, a
        if (b - a).days > 366:
            raise ValidationError("Custom ranges can be at most one year long.")
        return a, min(b, today)
    if range_key not in RANGES:
        raise ValidationError("Unknown range.")
    if range_key == "today":
        return today, today
    if range_key == "yesterday":
        y = today - dt.timedelta(days=1)
        return y, y
    return today - dt.timedelta(days=RANGES[range_key] - 1), today


class AnalyticsService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def series(self, guild_id: int, metrics: list[str], range_key: str = "7d", start: Optional[str] = None,
                     end: Optional[str] = None) -> dict:
        a, b = resolve_range(range_key, start, end)
        days = [(a + dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
        lo, hi = a.isoformat(), (b + dt.timedelta(days=1)).isoformat()
        out = {"range": {"start": lo, "end": b.isoformat(), "key": range_key}, "days": days, "metrics": {}}
        for m in metrics:
            if m not in METRICS:
                raise ValidationError(f"Unknown metric '{m}'.")
            label, sql = METRICS[m]
            rows = {r["d"]: r["v"] or 0 for r in await self.db.fetchall(sql, (guild_id, lo, hi))}
            values = [rows.get(d, 0) for d in days]
            since = await self.db.fetchval(TRACKING_SINCE[m], (guild_id,)) if m in TRACKING_SINCE else None
            out["metrics"][m] = {"label": label, "values": values, "total": round(sum(values), 1), "tracking_since": since}
        return out

    async def summary(self, guild_id: int, range_key: str = "7d", start: Optional[str] = None, end: Optional[str] = None) -> dict:
        data = await self.series(guild_id, list(METRICS), range_key, start, end)
        a, _ = resolve_range(range_key, start, end)
        since = a.isoformat()
        data["leaders"] = {"shifts": await self.p.shifts.leaderboard(guild_id, since, 10)}
        rows = await self.db.fetchall("SELECT command, COUNT(*) n FROM command_usage WHERE guild_id=? AND at>=? GROUP BY command "
                                      "ORDER BY n DESC LIMIT 10", (guild_id, since))
        data["leaders"]["commands"] = [{"command": r["command"], "count": r["n"]} for r in rows]
        rows = await self.db.fetchall("SELECT action, COUNT(*) n FROM mod_cases WHERE guild_id=? AND created_at>=? GROUP BY action", (guild_id, since))
        data["breakdowns"] = {"moderation": {r["action"]: r["n"] for r in rows}}
        rows = await self.db.fetchall("SELECT status, COUNT(*) n FROM applications WHERE guild_id=? AND created_at>=? GROUP BY status", (guild_id, since))
        data["breakdowns"]["applications"] = {r["status"]: r["n"] for r in rows}
        rows = await self.db.fetchall("SELECT provider, COUNT(*) n, ROUND(AVG(latency_ms)) ms, SUM(ok) ok FROM ai_usage WHERE guild_id=? AND at>=? "
                                      "GROUP BY provider ORDER BY n DESC", (guild_id, since))
        data["breakdowns"]["ai"] = [dict(r) for r in rows]
        return data

    async def export_csv(self, guild_id: int, metrics: list[str], range_key: str = "30d", start: Optional[str] = None,
                         end: Optional[str] = None) -> str:
        data = await self.series(guild_id, metrics or list(METRICS), range_key, start, end)
        buf = io.StringIO()
        w = csv.writer(buf)
        keys = list(data["metrics"])
        w.writerow(["date"] + [data["metrics"][k]["label"] for k in keys])
        for i, d in enumerate(data["days"]):
            w.writerow([d] + [data["metrics"][k]["values"][i] for k in keys])
        return buf.getvalue()

    async def record_command(self, guild_id: Optional[int], user_id: int, command: str, ok: bool = True) -> None:
        await self.db.execute("INSERT INTO command_usage(guild_id, user_id, command, ok) VALUES (?, ?, ?, ?)",
                              (guild_id, user_id, command[:60], int(ok)))
