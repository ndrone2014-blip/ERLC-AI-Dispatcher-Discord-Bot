"""Audit log: every important action is recorded in the database."""
from __future__ import annotations

import logging
from typing import Any, Optional

from database.connection import Database
from database.models import dumps

log = logging.getLogger("audit")

_SENSITIVE_KEYS = {"key", "api_key", "server_key", "token", "secret", "password"}


def _scrub(detail: Any) -> Any:
    if isinstance(detail, dict):
        return {k: ("***" if k.lower() in _SENSITIVE_KEYS else _scrub(v)) for k, v in detail.items()}
    if isinstance(detail, list):
        return [_scrub(v) for v in detail]
    return detail


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    async def record(
        self,
        guild_id: Optional[int],
        actor_id: Optional[int],
        action: str,
        target: Optional[str] = None,
        detail: Any = None,
    ) -> None:
        safe = _scrub(detail) if detail is not None else None
        log.info("guild=%s actor=%s action=%s target=%s", guild_id, actor_id, action, target)
        await self.db.execute(
            "INSERT INTO audit_log(guild_id, actor_id, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (guild_id, actor_id, action, target, dumps(safe) if safe is not None else None),
        )

    async def recent(self, guild_id: int, limit: int = 15) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)
        )
        return [dict(r) for r in rows]
