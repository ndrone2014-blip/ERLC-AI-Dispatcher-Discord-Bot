"""Centralised notifications: shown in the dashboard bell, optionally mirrored to Discord channels.

Duplicates (same dedupe key within 10 minutes) are dropped so a flapping integration
cannot flood staff.
"""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Iterable, Optional

from .common import clean_text, iso_now, row_dicts, utcnow

if TYPE_CHECKING:
    from .platform import CommunityPlatform

KINDS = {
    "application": "New applications",
    "moderation": "Important moderation events",
    "report": "Member reports",
    "dispatch": "Critical dispatch incidents",
    "staff": "Staff activity (leave requests, inactivity)",
    "verification": "Verification",
    "session": "Sessions",
    "automation": "Automation failures",
    "integration": "Integration failures (ER:LC, AI, Roblox)",
    "system": "System problems",
    "config": "Important configuration changes",
}
SEVERITY_COLOUR = {"info": 0x3CC8F0, "success": 0x2EA043, "warning": 0xD29922, "critical": 0xDA3633}
DEFAULTS = {"disabled": ["verification"], "channels": {}}


class NotificationService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "notification_settings", DEFAULTS)

    async def create(self, guild_id: int, kind: str, title: str, *, body: str = "", severity: str = "info",
                     link: Optional[str] = None, permission: Optional[str] = None, dedupe_key: Optional[str] = None,
                     window_seconds: int = 600) -> Optional[int]:
        s = await self.settings(guild_id)
        if kind in (s.get("disabled") or []):
            return None
        if dedupe_key:
            since = (utcnow() - dt.timedelta(seconds=window_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if await self.db.fetchone("SELECT 1 FROM notifications WHERE guild_id = ? AND dedupe_key = ? AND created_at >= ?",
                                      (guild_id, dedupe_key, since)):
                return None
        severity = severity if severity in SEVERITY_COLOUR else "info"
        nid = await self.db.execute(
            "INSERT INTO notifications(guild_id, kind, severity, title, body, link, permission, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, kind, severity, clean_text(title, 200), clean_text(body, 1000), link, permission, dedupe_key))
        await self.p.publish("notification.created", guild_id, notification_id=nid, kind=kind)
        channel_id = (s.get("channels") or {}).get(kind)
        if channel_id:
            embed = {"title": f"🔔 {clean_text(title, 200)}", "description": clean_text(body, 1500) or None,
                     "color": SEVERITY_COLOUR[severity], "footer": {"text": "Pulse Dispatch | ERLC • Notification"}}
            await self.p.actions.send(guild_id, int(channel_id), embed=embed)
        return nid

    async def list_for(self, guild_id: int, user_id: int, permissions: Iterable[str], *, limit: int = 30,
                       unread_only: bool = False) -> list[dict]:
        perms = set(permissions)
        rows = await self.db.fetchall(
            "SELECT n.*, (r.user_id IS NOT NULL) AS read FROM notifications n "
            "LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.user_id = ? "
            "WHERE n.guild_id = ? ORDER BY n.id DESC LIMIT 200", (user_id, guild_id))
        out = [r for r in row_dicts(rows) if not r.get("permission") or r["permission"] in perms]
        if unread_only:
            out = [r for r in out if not r["read"]]
        return out[:max(1, min(100, limit))]

    async def unread_count(self, guild_id: int, user_id: int, permissions: Iterable[str]) -> int:
        return len(await self.list_for(guild_id, user_id, permissions, limit=100, unread_only=True))

    async def mark_read(self, guild_id: int, user_id: int, ids: Optional[list[int]] = None) -> int:
        if ids is None:
            rows = await self.db.fetchall("SELECT id FROM notifications WHERE guild_id = ? ORDER BY id DESC LIMIT 200", (guild_id,))
            ids = [r["id"] for r in rows]
        n = 0
        for nid in ids:
            row = await self.db.fetchone("SELECT 1 FROM notifications WHERE id = ? AND guild_id = ?", (int(nid), guild_id))
            if row is None:
                continue  # never touch another server's notifications
            await self.db.execute("INSERT OR IGNORE INTO notification_reads(notification_id, user_id, read_at) VALUES (?, ?, ?)",
                                  (int(nid), user_id, iso_now()))
            n += 1
        return n
