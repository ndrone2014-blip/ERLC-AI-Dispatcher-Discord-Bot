"""/setup — create the dispatch channel layout and deploy all panels."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui import reply, require
from panels.base import PANEL_TITLES

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

PUBLIC_LAYOUT = [
    ("dispatch-dashboard", ["dashboard"]),
    ("active-calls", ["incidents"]),
    ("unit-board", ["units"]),
    ("bolo-board", ["bolos"]),
    ("dispatch-log", []),
    ("citizen-services", ["citizen"]),
]
PRIVATE_LAYOUT = [
    ("dispatch-control", ["voice", "erlc", "ai", "admin"]),
    ("police-reports", ["reports"]),
    ("department-notices", []),
    ("dispatch-stats", ["stats"]),
]


class SetupCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    setup = app_commands.Group(name="setup", description="Configure the dispatch system", guild_only=True)

    @setup.command(name="run", description="Create the dispatch channels and post every panel (safe to re-run)")
    @app_commands.describe(create_channels="Create categories/channels (False = post all panels in this channel)")
    async def run(self, interaction: discord.Interaction, create_channels: bool = True):
        if not await require(interaction, "admin.config"):
            return
        guild = interaction.guild
        me = guild.me
        await interaction.response.defer(ephemeral=True, thinking=True)
        if create_channels and not me.guild_permissions.manage_channels:
            return await interaction.followup.send("⚠️ I need the **Manage Channels** permission to create the layout.", ephemeral=True)
        await self.bot.core.units.ensure_defaults(guild.id, guild.name)
        locations: dict[str, int] = {}
        if not create_channels:
            for panel in PANEL_TITLES:
                await self.bot.panels.post(guild.id, panel, interaction.channel)
                locations[panel] = interaction.channel.id
                await asyncio.sleep(0.5)
            await self.bot.db.set_config(guild.id, "panel_channels", locations)
            for key in ("log_channel_id", "admin_channel_id", "department_channel_id"):
                if not await self.bot.db.get_config(guild.id, key):
                    await self.bot.db.set_config(guild.id, key, interaction.channel.id)
            return await interaction.followup.send("✅ All panels posted here. Set dedicated channels later in Administration → Channels.",
                                                   ephemeral=True)

        public_cat = await self._category(guild, "public_category_id", "📡 DISPATCH CENTER", {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False, create_public_threads=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True,
                                            connect=True, speak=True)})
        private_cat = await self._category(guild, "private_category_id", "🔒 DISPATCH ADMIN", {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True)})
        created = []
        for cat, layout in ((public_cat, PUBLIC_LAYOUT), (private_cat, PRIVATE_LAYOUT)):
            for name, panels in layout:
                ch = discord.utils.get(cat.text_channels, name=name)
                if ch is None:
                    ch = await guild.create_text_channel(name, category=cat, reason="ER:LC dispatch setup")
                    created.append(ch.mention)
                for p in panels:
                    locations[p] = ch.id
                if name == "dispatch-log":
                    await self.bot.db.set_config(guild.id, "log_channel_id", ch.id)
                elif name == "dispatch-control":
                    await self.bot.db.set_config(guild.id, "admin_channel_id", ch.id)
                elif name == "department-notices":
                    await self.bot.db.set_config(guild.id, "department_channel_id", ch.id)
        radio = discord.utils.get(public_cat.voice_channels, name="📻 Dispatch Radio")
        if radio is None:
            radio = await guild.create_voice_channel("📻 Dispatch Radio", category=public_cat, reason="ER:LC dispatch setup")
            created.append(radio.mention)
        vs = await self.bot.voice.settings(guild.id)
        if not vs["channel_id"]:
            await self.bot.voice.update_settings(guild.id, channel_id=radio.id)
        await self.bot.db.set_config(guild.id, "panel_channels", locations)
        await self.bot.apply_private_channel_permissions(guild)

        posted = []
        for panel, cid in locations.items():
            existing = await self.bot.panels.location(guild.id, panel)
            if existing and existing["channel_id"] == cid and await self.bot.panels.refresh(guild.id, panel, force=True):
                continue  # already there and working
            await self.bot.panels.post(guild.id, panel, self.bot.get_channel(cid))
            posted.append(PANEL_TITLES[panel])
            await asyncio.sleep(0.6)
        await self.bot.audit.record(guild.id, interaction.user.id, "setup.run", None, {"created": len(created), "posted": posted})
        await interaction.followup.send(
            "✅ **Dispatch system set up.**\n"
            f"Created: {', '.join(created) or 'nothing new'}\nPanels posted: {', '.join(posted) or 'all already present'}\n\n"
            "**Next steps**\n"
            "1. `#dispatch-control` → **ER:LC Integration → Set API Key**.\n"
            "2. **Administration → Roles & permissions**: map your Discord roles (Officer, Dispatcher, Supervisor, Admin…). "
            "Private channels unlock for mapped staff roles.\n"
            "3. **Voice Control**: pick 📻 Dispatch Radio to have the dispatcher join.\n"
            "4. Officers register on `#unit-board`.\n"
            "5. **Administration → Test mode** to run a TEST DATA call end-to-end.", ephemeral=True)

    async def _category(self, guild: discord.Guild, key: str, name: str, overwrites) -> discord.CategoryChannel:
        cid = await self.bot.db.get_config(guild.id, key)
        cat = guild.get_channel(int(cid)) if cid else None
        if not isinstance(cat, discord.CategoryChannel):
            cat = discord.utils.get(guild.categories, name=name) or await guild.create_category(name, overwrites=overwrites,
                                                                                                   reason="ER:LC dispatch setup")
            await self.bot.db.set_config(guild.id, key, cat.id)
        return cat

    @setup.command(name="repair", description="Recreate any panel message that was deleted")
    async def repair(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        restored = await self.bot.repair_panels(interaction.guild)
        await self.bot.panels.refresh_all(interaction.guild_id, force=True)
        await interaction.followup.send(f"🧰 Restored: {', '.join(restored) or 'nothing was missing'}. All panels refreshed.", ephemeral=True)

    @setup.command(name="panel", description="Post (or move) one panel into this channel")
    @app_commands.choices(panel=[app_commands.Choice(name=v, value=k) for k, v in PANEL_TITLES.items()])
    async def panel(self, interaction: discord.Interaction, panel: app_commands.Choice[str]):
        if not await require(interaction, "admin.config"):
            return
        await self.bot.panels.post(interaction.guild_id, panel.value, interaction.channel)
        locations = await self.bot.db.get_config(interaction.guild_id, "panel_channels", {}) or {}
        locations[panel.value] = interaction.channel.id
        await self.bot.db.set_config(interaction.guild_id, "panel_channels", locations)
        await reply(interaction, f"📌 {panel.name} posted here.")


async def setup(bot: "DispatchBot"):
    await bot.add_cog(SetupCog(bot))
