"""/setup — create the dispatch channel layout and (optionally) deploy dashboards.

Dashboards are optional (Thanksgiving Update). Presets:
  dispatch_only  - natural-language #dispatch channel + radio. No panels.
  dispatch_logs  - Dispatch Only + #dispatch-log (live incident cards and log embeds).
  full           - every channel and all 11 panels (the original layout).
  custom         - pick exactly which panels you want.
Logging is always available: every event is stored and viewable with /logs view, whatever the preset.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui import reply, require
from panels.base import PANEL_TITLES

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

PRESETS = {
    "dispatch_only": ("Dispatch Only", []),
    "dispatch_logs": ("Dispatch + Logs", []),
    "full": ("Full Dispatch System", list(PANEL_TITLES)),
    "custom": ("Custom Configuration", None),
}
PRESET_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in PRESETS.items()]

PUBLIC_LAYOUT = [
    ("dispatch-dashboard", ["dashboard"]),
    ("active-calls", ["incidents"]),
    ("unit-board", ["units"]),
    ("bolo-board", ["bolos"]),
    ("dispatch-log", []),
    ("citizen-services", ["citizen"]),
    ("dispatch", []),
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

    @setup.command(name="run", description="Set up Pulse Dispatch: choose Dispatch Only, + Logs, Full or Custom (safe to re-run)")
    @app_commands.describe(mode="Which features to set up (leave empty to choose with buttons)",
                           create_channels="Create categories/channels (False = use this channel)")
    @app_commands.choices(mode=PRESET_CHOICES)
    async def run(self, interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None,
                  create_channels: bool = True):
        if not await require(interaction, "admin.config"):
            return
        if mode is None:
            return await reply(interaction, embed=_preset_embed(), view=PresetPicker(self, create_channels))
        if mode.value == "custom":
            return await reply(interaction, "Pick the dashboards you want (you can change this later with `/dashboard`):",
                               view=CustomPanelPicker(self, create_channels))
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.apply(interaction, mode.value, PRESETS[mode.value][1], create_channels)

    async def apply(self, interaction: discord.Interaction, preset: str, panels: list[str], create_channels: bool) -> None:
        guild = interaction.guild
        me = guild.me
        if create_channels and not me.guild_permissions.manage_channels:
            return await interaction.followup.send("⚠️ I need the **Manage Channels** permission to create the layout.", ephemeral=True)
        await self.bot.core.units.ensure_defaults(guild.id, guild.name)
        old_enabled = set(await self.bot.panels.enabled_panels(guild.id))
        await self.bot.panels.set_enabled(guild.id, panels)
        await self.bot.db.set_config(guild.id, "setup_preset", preset)
        # Panels that are no longer wanted are removed so the server is not cluttered.
        removed = []
        for p in old_enabled - set(panels):
            if await self.bot.panels.remove(guild.id, p):
                removed.append(PANEL_TITLES[p])
        locations: dict[str, int] = {}
        created: list[str] = []
        if not create_channels:
            for panel in panels:
                await self.bot.panels.post(guild.id, panel, interaction.channel)
                locations[panel] = interaction.channel.id
                await asyncio.sleep(0.5)
            await self.bot.db.set_config(guild.id, "panel_channels", locations)
            keys = ["dispatch_channel_id"] + (["log_channel_id"] if preset != "dispatch_only" else [])
            if preset == "full" or panels:
                keys += ["admin_channel_id", "department_channel_id"]
            for key in keys:
                if not await self.bot.db.get_config(guild.id, key):
                    await self.bot.db.set_config(guild.id, key, interaction.channel.id)
            await self.bot.audit.record(guild.id, interaction.user.id, "setup.run", preset, {"panels": panels})
            return await interaction.followup.send(
                f"✅ **{PRESETS[preset][0]}** set up in this channel." + (f" Panels: {', '.join(PANEL_TITLES[p] for p in panels)}." if panels else
                                                                          " No dashboards were posted.")
                + "\nSet dedicated channels later with `/logs channel` and `/settings dispatch-channel`.", ephemeral=True)

        public_cat = await self._category(guild, "public_category_id", "📡 DISPATCH CENTER", {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False, create_public_threads=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True,
                                            connect=True, speak=True)})
        wanted_public = {"dispatch"} | ({"dispatch-log"} if preset != "dispatch_only" else set())
        wanted_private: set[str] = set()
        for name, ps in PUBLIC_LAYOUT:
            if set(ps) & set(panels):
                wanted_public.add(name)
        for name, ps in PRIVATE_LAYOUT:
            if set(ps) & set(panels) or (preset == "full"):
                wanted_private.add(name)
        if preset == "full":
            wanted_public |= {n for n, _ in PUBLIC_LAYOUT}
        private_cat = None
        if wanted_private:
            private_cat = await self._category(guild, "private_category_id", "🔒 DISPATCH ADMIN", {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True)})
        for cat, layout, wanted in ((public_cat, PUBLIC_LAYOUT, wanted_public), (private_cat, PRIVATE_LAYOUT, wanted_private)):
            if cat is None:
                continue
            for name, ps in layout:
                if name not in wanted:
                    continue
                ch = discord.utils.get(cat.text_channels, name=name)
                if ch is None:
                    ch = await guild.create_text_channel(name, category=cat, reason="Pulse Dispatch setup")
                    created.append(ch.mention)
                for p in ps:
                    if p in panels:
                        locations[p] = ch.id
                if name == "dispatch-log":
                    await self.bot.db.set_config(guild.id, "log_channel_id", ch.id)
                elif name == "dispatch-control":
                    await self.bot.db.set_config(guild.id, "admin_channel_id", ch.id)
                elif name == "department-notices":
                    await self.bot.db.set_config(guild.id, "department_channel_id", ch.id)
                elif name == "dispatch":
                    await self.bot.db.set_config(guild.id, "dispatch_channel_id", ch.id)
                    # Everyone may type here: this is where people talk to dispatch in plain language.
                    try:
                        await ch.set_permissions(guild.default_role, send_messages=True, reason="Pulse Dispatch: natural-language channel")
                        await ch.edit(topic="Talk to dispatch in plain language: describe an emergency and where it is, "
                                            "or (units) say things like 'show me 10-8'. Roleplay only.")
                    except discord.HTTPException:
                        pass
        radio = discord.utils.get(public_cat.voice_channels, name="📻 Dispatch Radio")
        if radio is None:
            radio = await guild.create_voice_channel("📻 Dispatch Radio", category=public_cat, reason="Pulse Dispatch setup")
            created.append(radio.mention)
        vs = await self.bot.voice.settings(guild.id)
        if not vs["channel_id"]:
            await self.bot.voice.update_settings(guild.id, channel_id=radio.id)
        await self.bot.db.set_config(guild.id, "panel_channels", locations)
        if private_cat is not None:
            await self.bot.apply_private_channel_permissions(guild)

        posted = []
        for panel, cid in locations.items():
            existing = await self.bot.panels.location(guild.id, panel)
            if existing and existing["channel_id"] == cid and await self.bot.panels.refresh(guild.id, panel, force=True):
                continue  # already there and working
            await self.bot.panels.post(guild.id, panel, self.bot.get_channel(cid))
            posted.append(PANEL_TITLES[panel])
            await asyncio.sleep(0.6)
        await self.bot.audit.record(guild.id, interaction.user.id, "setup.run", preset,
                                    {"created": len(created), "posted": posted, "removed": removed})
        dispatch_ch = await self.bot.channel_for(guild.id, "dispatch_channel_id")
        steps = [f"1. People describe emergencies in plain language in {dispatch_ch.mention if dispatch_ch else '#dispatch'} "
                 "(or use `/911`).",
                 "2. `/settings erlc` or the ER:LC panel → set your ER:LC API key for automatic in-game 911 calls.",
                 "3. `/ai-provider list` → add a free AI key (OpenRouter, Groq, Gemini…) — optional, dispatch works without AI.",
                 "4. `/permissions map` → map your Discord roles (Officer, Dispatcher, Supervisor, Admin).",
                 "5. Units register with `/unit-register`.",
                 "6. `/logs settings` → choose which events are logged. `/dashboard` → add dashboards any time."]
        await interaction.followup.send(
            f"✅ **Pulse Dispatch — {PRESETS[preset][0]}** is ready.\n"
            f"Created: {', '.join(created) or 'nothing new'}\n"
            f"Dashboards posted: {', '.join(posted) or ('none (dashboards are optional)' if not panels else 'all already present')}"
            + (f"\nRemoved: {', '.join(removed)}" if removed else "") + "\n\n**Next steps**\n" + "\n".join(steps),
            ephemeral=True)

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
        enabled = await self.bot.panels.enabled_panels(interaction.guild_id)
        if panel.value not in enabled:
            await self.bot.panels.set_enabled(interaction.guild_id, enabled + [panel.value])
        locations = await self.bot.db.get_config(interaction.guild_id, "panel_channels", {}) or {}
        locations[panel.value] = interaction.channel.id
        await self.bot.db.set_config(interaction.guild_id, "panel_channels", locations)
        await reply(interaction, f"📌 {panel.name} posted here.")


def _preset_embed() -> discord.Embed:
    from bot import embeds as E
    e = E.base("🛠️ Pulse Dispatch Setup", description="Choose what this server needs. Dashboards are optional and can be "
                                                    "added or removed later with `/dashboard`. **Logging is always available.**")
    e.add_field(name="📡 Dispatch Only", value="Plain-language #dispatch channel + radio. No panels.", inline=False)
    e.add_field(name="📋 Dispatch + Logs", value="Dispatch Only + #dispatch-log with live incident cards and log embeds.", inline=False)
    e.add_field(name="🖥️ Full Dispatch System", value="Every channel and all 11 dashboards (units, BOLOs, reports, stats…).", inline=False)
    e.add_field(name="🧩 Custom", value="Pick exactly which dashboards you want.", inline=False)
    return e


class PresetPicker(discord.ui.View):
    def __init__(self, cog: SetupCog, create_channels: bool):
        super().__init__(timeout=300)
        self.cog, self.create_channels = cog, create_channels
        for key, (label, _) in PRESETS.items():
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if key != "custom" else discord.ButtonStyle.secondary)
            b.callback = self._make(key)
            self.add_item(b)

    def _make(self, key: str):
        async def cb(interaction: discord.Interaction):
            if not await require(interaction, "admin.config"):
                return
            if key == "custom":
                return await interaction.response.edit_message(content="Pick the dashboards you want:", embed=None,
                                                               view=CustomPanelPicker(self.cog, self.create_channels))
            await interaction.response.edit_message(content=f"⏳ Setting up **{PRESETS[key][0]}**…", embed=None, view=None)
            await self.cog.apply(interaction, key, PRESETS[key][1], self.create_channels)
        return cb


class CustomPanelPicker(discord.ui.View):
    def __init__(self, cog: SetupCog, create_channels: bool):
        super().__init__(timeout=300)
        self.cog, self.create_channels = cog, create_channels
        sel = discord.ui.Select(placeholder="Choose dashboards (or none)…", min_values=0, max_values=len(PANEL_TITLES),
                                options=[discord.SelectOption(label=v, value=k) for k, v in PANEL_TITLES.items()])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        await interaction.response.edit_message(content="⏳ Applying your custom configuration…", view=None)
        await self.cog.apply(interaction, "custom", list(self.sel.values), self.create_channels)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(SetupCog(bot))
