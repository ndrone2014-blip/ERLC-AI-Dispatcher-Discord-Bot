"""Activity feed storage (v5.0).

Every logbook entry and every in-game join/leave is stored in `activity_events`, independent of
whether that log category posts to Discord. The dashboard's Activity feed reads from here.
No discord.py import: shared by the bot, the community services and the web API.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Feed filters shown in the dashboard: key -> (label, categories stored under it)
FEED_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "players": ("Player joins & leaves", ("erlc_players",)),
    "moderation": ("Moderation", ("moderation", "erlc_moderation")),
    "shifts": ("Staff & shifts", ("shifts", "staff")),
    "bolos": ("BOLOs", ("bolos",)),
    "sessions": ("Sessions", ("sessions",)),
    "applications": ("Applications", ("applications",)),
    "verification": ("Verification", ("verification",)),
    "dispatch": ("Dispatch", ("calls", "incidents", "closures", "assignments", "backup", "priority", "unit_status")),
    "members": ("Discord members", ("members", "roles")),
    "admin": ("Administrative", ("admin", "dashboard", "security", "automation", "erlc", "erlc_commands")),
}
# Who may see which categories in the feed (any one of the listed permissions).
CATEGORY_PERMS: dict[str, tuple[str, ...]] = {
    "erlc_players": ("erlc.view",), "erlc_moderation": ("erlc.view",), "erlc_commands": ("logs.view",),
    "moderation": ("mod.view",), "bolos": ("bolo.view",), "shifts": ("shift.self", "staff.view"), "staff": ("staff.view",),
    "sessions": ("session.view",), "applications": ("application.review",), "verification": ("members.view", "verification.manage"),
    "members": ("members.view",), "roles": ("members.view",),
    "calls": ("incident.view",), "incidents": ("incident.view",), "closures": ("incident.view",), "assignments": ("incident.view",),
    "backup": ("incident.view",), "priority": ("incident.view",), "unit_status": ("incident.view",),
    "admin": ("logs.view",), "dashboard": ("logs.view",), "security": ("logs.view",), "automation": ("logs.view",), "erlc": ("logs.view",),
}


def visible_categories(perms) -> list[str]:
    perms = set(perms)
    return [c for c, need in CATEGORY_PERMS.items() if any(p in perms for p in need)]


# Categories that are too chatty or private for a shared feed.
SKIP = {"radio", "ai_responses", "commands", "errors", "ai_failures", "failed"}
RETENTION_DAYS = 60


def link_for(category: str, title: str) -> Optional[str]:
    return {"moderation": "moderation/discord", "erlc_moderation": "moderation/records", "shifts": "staff/shifts",
            "staff": "staff", "bolos": "bolos", "sessions": "sessions", "applications": "applications",
            "verification": "verification", "erlc_players": "erlc/players", "calls": "dispatch", "incidents": "dispatch",
            "closures": "incidents", "assignments": "dispatch", "members": "members"}.get(category)


async def record(db, guild_id: Optional[int], category: str, title: str, *, body: str = "", actor_id: Optional[int] = None,
                 link: Optional[str] = None) -> None:
    """Store one activity event. Never raises: activity is a record, not part of the action."""
    if not guild_id or category in SKIP:
        return
    try:
        await db.execute("INSERT INTO activity_events(guild_id, category, title, body, actor_id, link) VALUES (?, ?, ?, ?, ?, ?)",
                         (int(guild_id), category[:40], (title or "")[:200], (body or "")[:600] or None,
                          int(actor_id) if actor_id else None, link or link_for(category, title)))
    except Exception:  # table missing during a partial migration, disk full, ...
        log.debug("Could not record activity event", exc_info=True)


def summarise(description: Optional[str], fields: Optional[list[tuple[str, Any, bool]]]) -> str:
    parts = []
    if description:
        parts.append(str(description).replace("\n", " ")[:200])
    for name, value, _inline in (fields or [])[:4]:
        if value:
            parts.append(f"{name}: {str(value).replace(chr(10), ' ')[:80]}")
    return " · ".join(parts)[:600]


async def prune(db, guild_id: int) -> int:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    n = await db.execute_rowcount("DELETE FROM activity_events WHERE guild_id = ? AND at < ?", (guild_id, cutoff))
    n += await db.execute_rowcount("DELETE FROM erlc_player_events WHERE guild_id = ? AND at < ?", (guild_id, cutoff))
    return n
