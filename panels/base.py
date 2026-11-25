"""Persistent, auto-updating Discord panels.

Each panel is one message whose channel/message IDs are stored in the `panels`
table. Updates are event-driven and debounced; a panel message is only edited
when its rendered content actually changed (respects Discord rate limits).
Deleted panel messages are detected and reported; `/setup repair` recreates them.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

from core import events as ev

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

Builder = Callable[["DispatchBot", int], Awaitable[tuple[list[discord.Embed], Optional[discord.ui.View]]]]

PANEL_TITLES = {
    "dashboard": "Main Dispatch Dashboard",
    "incidents": "Active Incidents",
    "units": "Unit Management",
    "voice": "Voice Control",
    "citizen": "Citizen Services",
    "reports": "Police Report Management",
    "bolos": "BOLO Management",
    "ai": "AI Configuration",
    "erlc": "ER:LC Integration",
    "admin": "Administration",
    "stats": "Statistics",
}

# Which events refresh which panels.
EVENT_PANELS = {
    ev.INCIDENT_CREATED: ["incidents", "dashboard", "stats"],
    ev.INCIDENT_UPDATED: ["incidents", "dashboard"],
    ev.INCIDENT_ASSESSED: ["incidents"],
    ev.INCIDENT_CLOSED: ["incidents", "dashboard", "stats", "units"],
    ev.UNIT_UPDATED: ["units", "dashboard", "stats"],
    ev.ASSIGNMENT_CHANGED: ["units", "incidents", "dashboard"],
    ev.BACKUP_REQUESTED: ["incidents", "dashboard"],
    ev.REPORT_CHANGED: ["reports", "stats"],
    ev.BOLO_CHANGED: ["bolos"],
    ev.ERLC_STATUS: ["erlc", "dashboard"],
    ev.VOICE_STATUS: ["voice", "dashboard"],
    ev.AI_STATUS: ["ai"],
    ev.CONFIG_CHANGED: ["admin", "ai", "voice", "units", "erlc"],
    ev.ACTIVITY: ["dashboard"],
    "announcement.updated": ["voice"],
}


class PanelManager:
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot
        self.builders: dict[str, Builder] = {}
        self._pending: dict[tuple[int, str], asyncio.Task] = {}
        self._hashes: dict[tuple[int, str], str] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self.debounce = 1.5
        for name, panels in EVENT_PANELS.items():
            bot.bus.subscribe(name, self._make_handler(panels))

    def register(self, name: str, builder: Builder) -> None:
        self.builders[name] = builder

    def _make_handler(self, panels: list[str]):
        async def handler(event: ev.Event) -> None:
            for p in panels:
                self.schedule(event.guild_id, p)
        return handler

    def schedule(self, guild_id: int, panel: str) -> None:
        key = (guild_id, panel)
        if key in self._pending and not self._pending[key].done():
            return
        self._pending[key] = asyncio.create_task(self._delayed(guild_id, panel))

    async def _delayed(self, guild_id: int, panel: str) -> None:
        await asyncio.sleep(self.debounce)
        try:
            await self.refresh(guild_id, panel)
        except Exception:
            log.exception("Panel refresh failed: %s/%s", guild_id, panel)

    async def location(self, guild_id: int, panel: str) -> Optional[dict]:
        row = await self.bot.db.fetchone("SELECT * FROM panels WHERE guild_id = ? AND panel = ?", (guild_id, panel))
        return dict(row) if row else None

    async def render(self, guild_id: int, panel: str) -> tuple[list[discord.Embed], Optional[discord.ui.View]]:
        return await self.builders[panel](self.bot, guild_id)

    @staticmethod
    def _digest(embeds: list[discord.Embed], view: Optional[discord.ui.View]) -> str:
        payload = [e.to_dict() for e in embeds]
        comps = view.to_components() if view else []
        return hashlib.sha256(json.dumps([payload, comps], sort_keys=True, default=str).encode()).hexdigest()

    async def refresh(self, guild_id: int, panel: str, force: bool = False) -> bool:
        loc = await self.location(guild_id, panel)
        if loc is None or panel not in self.builders:
            return False
        key = (guild_id, panel)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            embeds, view = await self.render(guild_id, panel)
            digest = self._digest(embeds, view)
            if not force and self._hashes.get(key) == digest:
                return True
            channel = self.bot.get_channel(loc["channel_id"])
            if channel is None:
                log.warning("Panel %s channel %s missing in guild %s", panel, loc["channel_id"], guild_id)
                return False
            try:
                msg = channel.get_partial_message(loc["message_id"])
                await msg.edit(embeds=embeds, view=view)
                self._hashes[key] = digest
                return True
            except discord.NotFound:
                log.warning("Panel %s message was deleted in guild %s", panel, guild_id)
                await self.bot.db.execute("DELETE FROM panels WHERE guild_id = ? AND panel = ?", (guild_id, panel))
                self._hashes.pop(key, None)
                await self.bot.notify_admins(guild_id, f"⚠️ The **{PANEL_TITLES.get(panel, panel)}** panel message was deleted. "
                                                       f"Run `/setup repair` to recreate it.")
                return False
            except discord.HTTPException as e:
                log.warning("Could not edit panel %s: %s", panel, e)
                return False

    async def post(self, guild_id: int, panel: str, channel: discord.abc.Messageable) -> discord.Message:
        embeds, view = await self.render(guild_id, panel)
        old = await self.location(guild_id, panel)
        if old:
            old_ch = self.bot.get_channel(old["channel_id"])
            if old_ch is not None:
                try:
                    await old_ch.get_partial_message(old["message_id"]).delete()
                except discord.HTTPException:
                    pass
        msg = await channel.send(embeds=embeds, view=view)
        await self.bot.db.execute(
            "INSERT INTO panels(guild_id, panel, channel_id, message_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, panel) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (guild_id, panel, msg.channel.id, msg.id))
        self._hashes[(guild_id, panel)] = self._digest(embeds, view)
        return msg

    async def refresh_all(self, guild_id: int, force: bool = False) -> None:
        for panel in self.builders:
            await self.refresh(guild_id, panel, force=force)
            await asyncio.sleep(0.3)

    async def enabled_panels(self, guild_id: int) -> list[str]:
        """Panels this server wants. Servers set up before the Thanksgiving Update keep every panel."""
        chosen = await self.bot.db.get_config(guild_id, "enabled_panels")
        if chosen is None:
            return list(self.builders)
        return [p for p in chosen if p in self.builders]

    async def set_enabled(self, guild_id: int, panels: list[str]) -> None:
        await self.bot.db.set_config(guild_id, "enabled_panels", [p for p in panels if p in self.builders])

    async def remove(self, guild_id: int, panel: str) -> bool:
        """Delete a panel message and forget it (used when a dashboard is disabled)."""
        loc = await self.location(guild_id, panel)
        await self.bot.db.execute("DELETE FROM panels WHERE guild_id = ? AND panel = ?", (guild_id, panel))
        self._hashes.pop((guild_id, panel), None)
        if loc:
            ch = self.bot.get_channel(loc["channel_id"])
            if ch is not None:
                try:
                    await ch.get_partial_message(loc["message_id"]).delete()
                except discord.HTTPException:
                    pass
        return loc is not None

    async def missing(self, guild_id: int) -> list[str]:
        rows = await self.bot.db.fetchall("SELECT panel FROM panels WHERE guild_id = ?", (guild_id,))
        have = {r["panel"] for r in rows}
        wanted = await self.enabled_panels(guild_id)
        return [p for p in wanted if p not in have]
