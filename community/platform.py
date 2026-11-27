"""CommunityPlatform: wires every v4.0 service together.

    platform = CommunityPlatform(db, bus, audit, permissions, actions=..., roblox=...)
    await platform.verification.start_profile(...)

The Discord bot and the web dashboard share one instance, so a change made in either place
is immediately visible in the other (same database, same event bus).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from core.events import EventBus
from database.connection import Database
from database.models import dumps
from security.audit import AuditLog
from security.permissions import PermissionManager

from .actions import DiscordActions

log = logging.getLogger(__name__)

LogWriter = Callable[..., Awaitable[None]]


class CommunityPlatform:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog, permissions: PermissionManager, *,
                 actions: Optional[DiscordActions] = None, roblox=None, ai=None, dispatch=None, secrets=None, env_keys=None):
        self.db = db
        self.bus = bus
        self.audit = audit
        self.permissions = permissions
        self.actions = actions or DiscordActions()
        self.ai = ai
        self.env_keys = {k: v for k, v in (env_keys or {}).items() if v}  # verification keys from .env
        self.secrets = secrets    # SecretStore (encrypted per-server keys, e.g. Bloxlink / RoVer API keys)
        self.dispatch = dispatch  # DispatchCore (optional; used by the assistant and analytics)
        self._log_writer: Optional[LogWriter] = None

        from .analytics import AnalyticsService
        from .applications import ApplicationService
        from .automation import AutomationEngine
        from .embeds import EmbedService
        from .members import MemberService
        from .moderation import ModerationService
        from .notifications import NotificationService
        from .roblox import RobloxAPI
        from .sessions import SessionService
        from .staff import ShiftService, StaffService
        from .verification import VerificationService

        self.notifications = NotificationService(self)
        self.embeds = EmbedService(self)
        self.members = MemberService(self)
        self.verification = VerificationService(self, roblox or RobloxAPI(None))
        self.staff = StaffService(self)
        self.shifts = ShiftService(self)
        self.sessions = SessionService(self)
        self.applications = ApplicationService(self)
        self.moderation = ModerationService(self)
        self.automation = AutomationEngine(self)
        self.analytics = AnalyticsService(self)
        from .assistant import Assistant
        self.assistant = Assistant(self)
        # v5.0 Platform Overhaul
        from .erlc_moderation import ErlcModerationService
        self.erlc_moderation = ErlcModerationService(self)
        bus.subscribe("bolo.changed", self._on_bolo)

    async def _on_bolo(self, e) -> None:
        """Tell authorized staff about new or reopened BOLOs (dashboard bell + optional Discord channel)."""
        if e.data.get("action") not in {"created", "reopened"} or self.dispatch is None:
            return
        b = await self.dispatch.bolos.get(e.data.get("bolo_id"))
        if not b:
            return
        sev = {"critical": "critical", "high": "warning"}.get(b.get("priority") or "", "info")
        who = f" ({b['roblox_username']})" if b.get("roblox_username") and b["roblox_username"].lower() != b["subject"].lower() else ""
        await self.notifications.create(b["guild_id"], "dispatch", f"BOLO #{b['number']} {e.data['action']}: {b['subject']}{who}",
                                        body=(b.get("reason") or b.get("description") or "")[:200], severity=sev,
                                        link=f"#/bolos/{b['number']}", permission="bolo.view")

    # ------------------------------------------------------------------ logging
    def set_log_writer(self, writer: LogWriter) -> None:
        """The bot replaces the default with Logbook.write (which also posts the Discord embed)."""
        self._log_writer = writer

    async def log(self, guild_id: int, category: str, title: str, *, description: Optional[str] = None,
                  fields: Optional[list[tuple[str, str, bool]]] = None, colour: Optional[int] = None,
                  actor_id: Optional[int] = None, incident_id: Optional[int] = None) -> None:
        try:
            if self._log_writer is None:  # the bot's Logbook records activity itself
                from core import activity
                await activity.record(self.db, guild_id, category, title, body=activity.summarise(description, fields),
                                      actor_id=actor_id)
            if self._log_writer is not None:
                await self._log_writer(guild_id, category, title, description=description, fields=fields, colour=colour,
                                       actor_id=actor_id, incident_id=incident_id)
                return
            detail = {"description": (description or "")[:1500], "fields": [(n, (v or "")[:500], i) for n, v, i in (fields or [])]}
            await self.db.execute(
                "INSERT INTO dispatch_log(guild_id, category, title, detail, incident_id, actor_id) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, category, title[:200], dumps(detail), incident_id, actor_id))
        except Exception:  # logging must never break the action that is being logged
            log.exception("Could not write %s log entry", category)

    # ------------------------------------------------------------------ settings
    async def settings(self, guild_id: int, key: str, defaults: dict[str, Any]) -> dict:
        stored = await self.db.get_config(guild_id, key, {}) or {}
        return {**defaults, **{k: v for k, v in stored.items() if k in defaults}}

    async def update_settings(self, guild_id: int, key: str, defaults: dict[str, Any], changes: dict[str, Any],
                              actor_id: Optional[int] = None) -> dict:
        current = await self.settings(guild_id, key, defaults)
        unknown = set(changes) - set(defaults)
        if unknown:
            from security.validation import ValidationError
            raise ValidationError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        current.update(changes)
        await self.db.set_config(guild_id, key, current)
        await self.audit.record(guild_id, actor_id, f"settings.{key}", None, changes)
        await self.bus.publish("config.changed", guild_id, key=key)
        return current

    async def publish(self, name: str, guild_id: int, **data: Any) -> None:
        await self.bus.publish(name, guild_id, **data)
