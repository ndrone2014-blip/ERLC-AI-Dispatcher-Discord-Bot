"""In-process async event bus used to decouple dispatch logic from panels/voice."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None]]


@dataclass
class Event:
    name: str
    guild_id: int
    data: dict[str, Any] = field(default_factory=dict)


# Event names (kept as constants to avoid typos)
INCIDENT_CREATED = "incident.created"
INCIDENT_UPDATED = "incident.updated"
INCIDENT_CLOSED = "incident.closed"
INCIDENT_ASSESSED = "incident.assessed"
UNIT_UPDATED = "unit.updated"
ASSIGNMENT_CHANGED = "assignment.changed"
BACKUP_REQUESTED = "backup.requested"
REPORT_CHANGED = "report.changed"
BOLO_CHANGED = "bolo.changed"
ERLC_STATUS = "erlc.status"
VOICE_STATUS = "voice.status"
AI_STATUS = "ai.status"
CONFIG_CHANGED = "config.changed"
ACTIVITY = "activity"


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []

    def subscribe(self, name: str, handler: Handler) -> None:
        if name == "*":
            self._wildcard.append(handler)
        else:
            self._handlers[name].append(handler)

    async def publish(self, name: str, guild_id: int, **data: Any) -> None:
        event = Event(name=name, guild_id=guild_id, data=data)
        handlers = list(self._handlers.get(name, [])) + list(self._wildcard)
        for handler in handlers:
            # Each handler runs independently so one failure never blocks dispatch.
            asyncio.create_task(self._safe(handler, event))

    @staticmethod
    async def _safe(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:  # pragma: no cover - defensive
            log.exception("Event handler %r failed for %s", handler, event.name)
