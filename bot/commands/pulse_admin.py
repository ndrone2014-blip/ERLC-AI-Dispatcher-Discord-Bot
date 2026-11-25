"""Administration commands (Thanksgiving Update):
/settings /dashboard /logs /departments /permissions /reset-settings.

Every value is stored per guild; one server's configuration never affects another.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.commands.pulse_dispatch import ConfirmView
from bot.logbook import LOG_CATEGORIES
from bot.ui import reply, require
from database.models import DEPARTMENT_KINDS, PRIORITIES, PRIORITY_LABEL, discord_ts
from dispatch import phrases
from panels.base import PANEL_TITLES
from security.permissions import DEFAULT_PERMISSIONS, GROUP_LABEL, GROUPS, PERMISSION_DESCRIPTIONS
from security.validation import ValidationError, clean_text

if TYPE_CHECKING:
    from bot.client import DispatchBot

PANEL_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in PANEL_TITLES.items()]
LOG_CHOICES = [app_commands.Choice(name=f"{icon} {label}"[:100], value=k) for k, (label, icon) in LOG_CATEGORIES.items()]
GROUP_CHOICES = [app_commands.Choice(name=GROUP_LABEL[g], value=g) for g in GROUPS if g not in {"owner", "citizen"}]
KIND_CHOICES = [app_commands.Choice(name=v[:100], value=k) for k, v in DEPARTMENT_KINDS.items()]
PRIORITY_CHOICES = [app_commands.Choice(name=PRIORITY_LABEL[p], value=p) for p in PRIORITIES]


async def _permission_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    out = [app_commands.Choice(name=f"{p} — {PERMISSION_DESCRIPTIONS.get(p, '')}"[:100], value=p)
           for p in DEFAULT_PERMISSIONS if cur in p or cur in PERMISSION_DESCRIPTIONS.get(p, "").lower()]
    return out[:25]


async def _category_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    rules = await interaction.client.db.get_config(interaction.guild_id, "custom_categories", []) or []
    return [app_commands.Choice(name=r["category"][:100], value=r["category"][:100]) for r in rules
            if current.lower() in r["category"].lower()][:25]


async def _status_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    codes = await interaction.client.core.units.status_codes(interaction.guild_id)
    return [app_commands.Choice(name=f"{c['code']} — {c['label']}"[:100], value=c["code"]) for c in codes
            if current.lower() in f"{c['code']} {c['label']}".lower()][:25]


class LogToggleView(discord.ui.View):
    """Multi-select of enabled log categories (shown by /logs settings)."""

    def __init__(self, enabled: dict[str, bool]):
        super().__init__(timeout=300)
        sel = discord.ui.Select(placeholder="Select the events to log…", min_values=0, max_values=len(LOG_CATEGORIES),
                                options=[discord.SelectOption(label=label[:100], value=k, emoji=icon, default=enabled.get(k, True))
                                         for k, (label, icon) in LOG_CATEGORIES.items()])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        bot = interaction.client
        chosen = set(self.sel.values)
        for k in LOG_CATEGORIES:
            await bot.logbook.set_category(interaction.guild_id, k, k in chosen)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "logs.categories", None, {"enabled": sorted(chosen)})
        await interaction.response.edit_message(content=f"✅ Logging {len(chosen)}/{len(LOG_CATEGORIES)} event types. "
                                                        "Disabled events are not posted or stored.", view=None)


class PulseAdminCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    # ================================================================== /settings
    settings = app_commands.Group(name="settings", description="Pulse Dispatch server settings (administrators)", guild_only=True)

    async def _set(self, interaction: discord.Interaction, key: str, value, label: str) -> None:
        await self.bot.core.set_setting(interaction.guild_id, key, value)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, f"settings.{key}", None, {"value": value})
        await reply(interaction, f"✅ {label}")

    @settings.command(name="view", description="Show every Pulse Dispatch setting for this server")
    async def settings_view(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        bot, gid = self.bot, interaction.guild_id
        core = bot.core
        look = await E.look_for(bot, gid)
        e = E.base("⚙️ Pulse Dispatch Settings", look=look)
        dch = await bot.channel_for(gid, "dispatch_channel_id")
        lch = await bot.log_channel(gid)
        e.add_field(name="Dispatch", value=(
            f"Response style: **{await core.setting(gid, 'response_style')}**\n"
            f"Agency name: {E.safe(await core.setting(gid, 'agency_name')) or '—'}\n"
            f"Dispatch channel: {dch.mention if dch else 'not set'} ({'on' if await core.setting(gid, 'text_dispatch_enabled') else 'off'})\n"
            f"Follow-up questions: {'on' if await core.setting(gid, 'follow_up_questions') else 'off'}\n"
            f"Merge duplicate reports: {'on' if await core.setting(gid, 'merge_duplicates') else 'off'} "
            f"({int(await core.setting(gid, 'duplicate_window_s')) // 60} min window)\n"
            f"Supervisor approval from: **{PRIORITY_LABEL.get(await core.setting(gid, 'priority_approval_threshold'), '?')}** priority"),
            inline=False)
        ai = await bot.ai.ai_settings(gid)
        active = bot.ai.active_provider(gid)
        e.add_field(name="AI", value=(f"Mode: **{ai['mode']}** · Failover: **{'on' if ai['failover'] else 'off'}**\n"
                                      f"Preferred: {ai.get('preferred') or '—'} · Last used: {active['label'] if active else '—'}"),
                    inline=False)
        log_s = await bot.logbook.settings(gid)
        on = sum(1 for v in log_s["categories"].values() if v)
        enabled_panels = await bot.panels.enabled_panels(gid)
        e.add_field(name="Logging & dashboards", value=(
            f"Log channel: {lch.mention if lch else 'not set (logs are still stored: /logs view)'}\n"
            f"Log events: {on}/{len(LOG_CATEGORIES)} enabled\n"
            f"Setup preset: {await bot.db.get_config(gid, 'setup_preset', 'full (legacy)')}\n"
            f"Dashboards: {', '.join(PANEL_TITLES[p] for p in enabled_panels) or 'none'}"), inline=False)
        st = await bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (gid,))
        if st is not None:
            st = dict(st)
            erlc_line = (("🟢 Connected" if st.get("connected") else "🔴 Not connected")
                         + (f" · {E.safe(st.get('server_name'), 40)}" if st.get("server_name") else "")
                         + (f"\nLast error {discord_ts(st.get('last_error_at'))}: {E.safe(st.get('last_error'), 150)}"
                            if st.get("last_error") and not st.get("connected") else ""))
        else:
            erlc_line = "Not configured — `/settings erlc-key`"
        e.add_field(name="ER:LC API", value=erlc_line[:1024], inline=False)
        custom = await bot.db.get_config(gid, "custom_categories", []) or []
        es = await bot.db.get_config(gid, "embed_settings", {}) or {}
        e.add_field(name="Customization", value=(
            f"Callsign format: {await bot.db.get_config(gid, 'callsign_format', 'any')}\n"
            f"Custom incident categories: {', '.join(c['category'] for c in custom) or 'none'}\n"
            f"Embed accent: #{int(es.get('accent', E.BRAND)):06X} · compact: {bool(es.get('compact'))}"), inline=False)
        await reply(interaction, embed=e)

    @settings.command(name="response-style", description="How dispatch talks: professional, concise or detailed")
    @app_commands.choices(style=[app_commands.Choice(name=f"{k.title()} — {v}", value=k) for k, v in phrases.STYLES.items()])
    async def settings_style(self, interaction: discord.Interaction, style: app_commands.Choice[str]):
        if await require(interaction, "admin.config"):
            await self._set(interaction, "response_style", style.value, f"Dispatch response style: **{style.value}**.")

    @settings.command(name="agency-name", description="Your dispatch center's name, used on the radio (leave empty to clear)")
    async def settings_agency(self, interaction: discord.Interaction, name: Optional[str] = None):
        if await require(interaction, "admin.config"):
            value = clean_text(name, 60)
            await self._set(interaction, "agency_name", value, f"Agency name: **{value or 'cleared'}**.")

    @settings.command(name="dispatch-channel", description="Channel where people talk to dispatch in plain language (empty = off)")
    async def settings_dispatch_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await require(interaction, "admin.config"):
            return
        if channel is None:
            await self.bot.db.delete_config(interaction.guild_id, "dispatch_channel_id")
            await self.bot.audit.record(interaction.guild_id, interaction.user.id, "settings.dispatch_channel", None)
            return await reply(interaction, "Natural-language dispatch channel turned off. `/dispatch ask` still works.")
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history):
            return await reply(interaction, f"⚠️ I need View, Send Messages, Embed Links and Read History in {channel.mention}.",
                               error=True)
        await self.bot.db.set_config(interaction.guild_id, "dispatch_channel_id", channel.id)
        await self.bot.core.set_setting(interaction.guild_id, "text_dispatch_enabled", True)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "settings.dispatch_channel", f"#{channel.name}")
        await reply(interaction, f"📡 People can now talk to dispatch in {channel.mention} — e.g. *“crash at the bank, need EMS”*.")

    @settings.command(name="follow-up-questions", description="Ask callers for a missing location or nature instead of guessing")
    async def settings_follow_up(self, interaction: discord.Interaction, enabled: bool):
        if await require(interaction, "admin.config"):
            await self._set(interaction, "follow_up_questions", enabled, f"Follow-up questions {'on' if enabled else 'off'}.")

    @settings.command(name="duplicates", description="Merge reports that match an active incident at the same place")
    @app_commands.describe(window_minutes="How recent an incident must be to be considered the same event")
    async def settings_duplicates(self, interaction: discord.Interaction, enabled: bool,
                                  window_minutes: app_commands.Range[int, 1, 240] = 30):
        if not await require(interaction, "admin.config"):
            return
        await self.bot.core.set_setting(interaction.guild_id, "merge_duplicates", enabled)
        await self._set(interaction, "duplicate_window_s", int(window_minutes) * 60,
                        f"Duplicate merging {'on' if enabled else 'off'} ({window_minutes} min window).")

    @settings.command(name="approval-threshold", description="Priorities at or above this need supervisor approval")
    @app_commands.choices(priority=PRIORITY_CHOICES)
    async def settings_threshold(self, interaction: discord.Interaction, priority: app_commands.Choice[str]):
        if await require(interaction, "admin.config"):
            await self._set(interaction, "priority_approval_threshold", priority.value,
                            f"Supervisor approval required from **{priority.name}** priority.")

    @settings.command(name="embed", description="Embed appearance: accent colour (#hex), compact mode, footer note")
    async def settings_embed(self, interaction: discord.Interaction, accent: Optional[str] = None, compact: Optional[bool] = None,
                             footer_note: Optional[str] = None):
        if not await require(interaction, "admin.config"):
            return
        es = await self.bot.db.get_config(interaction.guild_id, "embed_settings", {}) or {}
        if accent is not None:
            m = re.fullmatch(r"#?([0-9a-fA-F]{6})", accent.strip())
            if not m:
                return await reply(interaction, "Accent must be a hex colour like `#1F6FEB`.", error=True)
            es["accent"] = int(m.group(1), 16)
        if compact is not None:
            es["compact"] = compact
        if footer_note is not None:
            es["footer_note"] = clean_text(footer_note, 60)
        await self.bot.db.set_config(interaction.guild_id, "embed_settings", es)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "settings.embed", None, es)
        look = await E.look_for(self.bot, interaction.guild_id)
        await reply(interaction, "✅ Embed appearance saved. Preview:",
                    embed=E.base("📡 Dispatch", description="Copy, 911 call received. Units are being notified.", look=look))

    @settings.command(name="callsign-format", description="Rule for unit identifiers when officers register")
    @app_commands.choices(rule=[app_commands.Choice(name="Any callsign", value="any"),
                                app_commands.Choice(name="Must start with the department code (PD-12)", value="department_prefix"),
                                app_commands.Choice(name="Numbers only (1-23, 412)", value="numeric")])
    async def settings_callsign(self, interaction: discord.Interaction, rule: app_commands.Choice[str]):
        if not await require(interaction, "admin.config"):
            return
        await self.bot.db.set_config(interaction.guild_id, "callsign_format", rule.value)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "settings.callsign_format", rule.value)
        await reply(interaction, f"✅ Callsign format: **{rule.name}** (applies to new registrations).")

    @settings.command(name="category-add", description="Add a server-specific incident category recognised from keywords")
    @app_commands.describe(keywords="Comma-separated words/phrases, e.g. 'gang war, turf war'",
                           category="Name shown on incidents", units="Recommended units",
                           departments="Comma-separated: law, fire, ems, supervisor, special")
    @app_commands.choices(priority=PRIORITY_CHOICES)
    async def settings_category_add(self, interaction: discord.Interaction, keywords: str, category: str,
                                    priority: app_commands.Choice[str], units: app_commands.Range[int, 1, 10] = 1,
                                    departments: str = "law"):
        if not await require(interaction, "admin.config"):
            return
        kws = [k.strip().lower() for k in clean_text(keywords, 300, required=True, field="Keywords").split(",") if k.strip()][:15]
        name = clean_text(category, 60, required=True, field="Category")
        kinds = [k.strip().lower() for k in departments.split(",") if k.strip().lower() in {"law", "fire", "ems", "supervisor", "special"}]
        rules = [r for r in (await self.bot.db.get_config(interaction.guild_id, "custom_categories", []) or [])
                 if r["category"].lower() != name.lower()]
        if len(rules) >= 25:
            return await reply(interaction, "A server can have at most 25 custom categories.", error=True)
        rules.append({"keywords": kws, "category": name, "priority": priority.value, "units": int(units), "kinds": kinds or ["law"]})
        await self.bot.db.set_config(interaction.guild_id, "custom_categories", rules)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "settings.category_add", name, rules[-1])
        await reply(interaction, f"✅ Category **{name}** ({priority.name}, {units} unit(s), {', '.join(kinds or ['law'])}) "
                                 f"is recognised from: {', '.join(kws)}.")

    @settings.command(name="category-remove", description="Remove a server-specific incident category")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def settings_category_remove(self, interaction: discord.Interaction, category: str):
        if not await require(interaction, "admin.config"):
            return
        rules = await self.bot.db.get_config(interaction.guild_id, "custom_categories", []) or []
        kept = [r for r in rules if r["category"].lower() != category.lower()]
        await self.bot.db.set_config(interaction.guild_id, "custom_categories", kept)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "settings.category_remove", category)
        await reply(interaction, "✅ Removed." if len(kept) != len(rules) else "No such category.")

    @settings.command(name="erlc-key", description="Set the ER:LC private server API key (stored encrypted)")
    async def settings_erlc_key(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.erlc"):
            return
        from panels.erlc import KeyModal
        await interaction.response.send_modal(KeyModal())

    # ================================================================== /dashboard
    dashboard = app_commands.Group(name="dashboard", description="Optional dashboards (panels) — enable only what you need",
                                   guild_only=True)

    @dashboard.command(name="status", description="Which dashboards are enabled and where they are")
    async def dashboard_status(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        enabled = set(await self.bot.panels.enabled_panels(interaction.guild_id))
        lines = []
        for key, title in PANEL_TITLES.items():
            loc = await self.bot.panels.location(interaction.guild_id, key)
            ch = self.bot.get_channel(loc["channel_id"]) if loc else None
            state = "🟢" if key in enabled and loc else ("🟡 enabled, not posted" if key in enabled else "⚫ off")
            lines.append(f"{state} **{title}**" + (f" — {ch.mention}" if ch else ""))
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🖥️ Dashboards", description="\n".join(lines), look=look)
        e.add_field(name="Change", value="`/dashboard enable` · `/dashboard disable` · `/dashboard preset`\n"
                                         "Dispatch and logging work the same with every dashboard off.", inline=False)
        await reply(interaction, embed=e)

    @dashboard.command(name="enable", description="Enable a dashboard and post it (in this channel or the one you pick)")
    @app_commands.choices(panel=PANEL_CHOICES)
    async def dashboard_enable(self, interaction: discord.Interaction, panel: app_commands.Choice[str],
                               channel: Optional[discord.TextChannel] = None):
        if not await require(interaction, "admin.config"):
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await reply(interaction, "Pick a text channel.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        enabled = await self.bot.panels.enabled_panels(interaction.guild_id)
        if panel.value not in enabled:
            await self.bot.panels.set_enabled(interaction.guild_id, enabled + [panel.value])
        try:
            await self.bot.panels.post(interaction.guild_id, panel.value, target)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"⚠️ Could not post in {target.mention}: {e.text or e}", ephemeral=True)
        locations = await self.bot.db.get_config(interaction.guild_id, "panel_channels", {}) or {}
        locations[panel.value] = target.id
        await self.bot.db.set_config(interaction.guild_id, "panel_channels", locations)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "dashboard.enable", panel.value)
        await interaction.followup.send(f"🟢 **{panel.name}** enabled in {target.mention}.", ephemeral=True)

    @dashboard.command(name="disable", description="Disable a dashboard and remove its message")
    @app_commands.choices(panel=PANEL_CHOICES)
    async def dashboard_disable(self, interaction: discord.Interaction, panel: app_commands.Choice[str]):
        if not await require(interaction, "admin.config"):
            return
        enabled = [p for p in await self.bot.panels.enabled_panels(interaction.guild_id) if p != panel.value]
        await self.bot.panels.set_enabled(interaction.guild_id, enabled)
        await self.bot.panels.remove(interaction.guild_id, panel.value)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "dashboard.disable", panel.value)
        await reply(interaction, f"⚫ **{panel.name}** disabled. Everything it showed is still available through commands.")

    @dashboard.command(name="preset", description="Apply a preset: Dispatch Only, Dispatch + Logs, Full or Custom")
    async def dashboard_preset(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        from bot.commands.setup import PresetPicker, _preset_embed
        cog = self.bot.get_cog("SetupCog")
        await reply(interaction, embed=_preset_embed(), view=PresetPicker(cog, True))

    # ================================================================== /logs
    logs = app_commands.Group(name="logs", description="Dispatch logging (always available)", guild_only=True)

    @logs.command(name="settings", description="Show logging configuration and choose which events are logged")
    async def logs_settings(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        gid = interaction.guild_id
        s = await self.bot.logbook.settings(gid)
        main = await self.bot.log_channel(gid)
        lines = []
        for k, (label, icon) in LOG_CATEGORIES.items():
            ch_id = s["channels"].get(k)
            ch = self.bot.get_channel(int(ch_id)) if ch_id else None
            lines.append(f"{'✅' if s['categories'].get(k, True) else '⬜'} {icon} {label}" + (f" → {ch.mention}" if ch else ""))
        look = await E.look_for(self.bot, gid)
        e = E.base("📋 Logging", description="\n".join(lines), look=look)
        e.add_field(name="Main log channel", value=main.mention if main else "Not set — logs are stored and viewable with `/logs view`.",
                    inline=False)
        e.add_field(name="Commands", value="`/logs channel` · `/logs create` · `/logs route` · `/logs toggle` · `/logs view`",
                    inline=False)
        await reply(interaction, embed=e, view=LogToggleView(s["categories"]))

    @logs.command(name="channel", description="Use an existing channel as the main log channel")
    async def logs_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require(interaction, "admin.config"):
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await reply(interaction, f"⚠️ I need View, Send Messages and Embed Links in {channel.mention}.", error=True)
        await self.bot.db.set_config(interaction.guild_id, "log_channel_id", channel.id)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "logs.channel", f"#{channel.name}")
        await reply(interaction, f"📋 Logs will be posted in {channel.mention}.")

    @logs.command(name="create", description="Create a dedicated private #pulse-logs channel")
    async def logs_create(self, interaction: discord.Interaction, name: str = "pulse-logs"):
        if not await require(interaction, "admin.config"):
            return
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_channels:
            return await reply(interaction, "⚠️ I need the **Manage Channels** permission.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
        roles = await self.bot.permissions.role_map(guild.id)
        for group in ("admin", "supervisor", "dispatcher"):
            for rid in roles.get(group, []):
                role = guild.get_role(rid)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        clean = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))[:90] or "pulse-logs"
        cat_id = await self.bot.db.get_config(guild.id, "private_category_id")
        cat = guild.get_channel(int(cat_id)) if cat_id else None
        ch = await guild.create_text_channel(clean, overwrites=overwrites, category=cat if isinstance(cat, discord.CategoryChannel) else None,
                                             reason="Pulse Dispatch log channel")
        await self.bot.db.set_config(guild.id, "log_channel_id", ch.id)
        await self.bot.audit.record(guild.id, interaction.user.id, "logs.create", f"#{ch.name}")
        await interaction.followup.send(f"📋 Created {ch.mention} (staff only) and set it as the log channel.", ephemeral=True)

    @logs.command(name="toggle", description="Enable or disable one log event type")
    @app_commands.choices(category=LOG_CHOICES)
    async def logs_toggle(self, interaction: discord.Interaction, category: app_commands.Choice[str], enabled: bool):
        if not await require(interaction, "admin.config"):
            return
        await self.bot.logbook.set_category(interaction.guild_id, category.value, enabled)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "logs.toggle", category.value, {"enabled": enabled})
        await reply(interaction, f"{'✅' if enabled else '⬜'} {category.name} logging {'enabled' if enabled else 'disabled'}.")

    @logs.command(name="route", description="Send one event type to a different channel (empty = main log channel)")
    @app_commands.choices(category=LOG_CHOICES)
    async def logs_route(self, interaction: discord.Interaction, category: app_commands.Choice[str],
                         channel: Optional[discord.TextChannel] = None):
        if not await require(interaction, "admin.config"):
            return
        await self.bot.logbook.set_category_channel(interaction.guild_id, category.value, channel.id if channel else None)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "logs.route", category.value,
                                    {"channel": channel.name if channel else None})
        await reply(interaction, f"{category.name} → {channel.mention if channel else 'main log channel'}.")

    @logs.command(name="view", description="View previous dispatch records (works even without a log channel)")
    @app_commands.choices(category=LOG_CHOICES)
    async def logs_view(self, interaction: discord.Interaction, category: Optional[app_commands.Choice[str]] = None,
                        count: app_commands.Range[int, 1, 25] = 15):
        if not await require(interaction, "report.view_all"):
            return
        rows = await self.bot.logbook.recent(interaction.guild_id, category.value if category else None, count)
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = []
        for r in rows:
            icon = LOG_CATEGORIES.get(r["category"], ("", "📋"))[1]
            who = f" · <@{r['actor_id']}>" if r.get("actor_id") else ""
            extra = ""
            fields = r["detail"].get("fields") or []
            if fields:
                extra = " — " + " · ".join(f"{f[1]}" for f in fields[:2] if f and len(f) > 1)
            lines.append(f"{discord_ts(r['at'], 'f')} {icon} **{E.safe(r['title'], 80)}**{E.safe(extra, 140)}{who}")
        e = E.base(f"📜 Dispatch Records{' · ' + category.name if category else ''}", description="\n".join(lines) or "No records yet.",
                   look=look)
        await reply(interaction, embed=e)

    # ================================================================== /departments
    departments = app_commands.Group(name="departments", description="Departments and unit statuses", guild_only=True)

    @departments.command(name="list", description="List departments and unit status codes")
    async def departments_list(self, interaction: discord.Interaction):
        if not await require(interaction, "incident.view"):
            return
        deps = await self.bot.core.units.departments(interaction.guild_id)
        codes = await self.bot.core.units.status_codes(interaction.guild_id)
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🏢 Departments", look=look, description="\n".join(
            f"`{d['short_code']:<5}` **{E.safe(d['name'], 50)}** · {d['kind']}" + (f" · <@&{d['role_id']}>" if d.get("role_id") else "")
            for d in deps) or "None configured.")
        e.add_field(name="Unit statuses", value="\n".join(f"`{c['code']}` {E.safe(c['label'], 40)} · {c['availability']}" for c in codes)[:1024]
                    or "None", inline=False)
        await reply(interaction, embed=e)

    @departments.command(name="add", description="Add or update a department (police, sheriff, state, fire, EMS, specialized…)")
    @app_commands.choices(kind=KIND_CHOICES)
    async def departments_add(self, interaction: discord.Interaction, name: str, code: str, kind: app_commands.Choice[str],
                              role: Optional[discord.Role] = None):
        if not await require(interaction, "admin.config"):
            return
        try:
            d = await self.bot.core.units.add_department(interaction.guild_id, name, code, kind.value, interaction.user.id,
                                                         role.id if role else None)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"🏢 Department `{d['short_code']}` **{E.safe(d['name'])}** ({d['kind']}) saved.")

    @departments.command(name="remove", description="Deactivate a department (existing records are kept)")
    async def departments_remove(self, interaction: discord.Interaction, code: str):
        if not await require(interaction, "admin.config"):
            return
        ok = await self.bot.core.units.remove_department(interaction.guild_id, code, interaction.user.id)
        await reply(interaction, "Department deactivated." if ok else "No department with that code.")

    @departments.command(name="status-add", description="Add or rename a unit status (e.g. 10-19 Returning to Station)")
    @app_commands.choices(availability=[app_commands.Choice(name="Available for calls", value="available"),
                                        app_commands.Choice(name="Assigned / on a call", value="assigned"),
                                        app_commands.Choice(name="Busy / unavailable", value="unavailable"),
                                        app_commands.Choice(name="Off duty / out of service", value="off")])
    async def status_add(self, interaction: discord.Interaction, code: str, label: str, availability: app_commands.Choice[str]):
        if not await require(interaction, "admin.config"):
            return
        try:
            await self.bot.core.units.save_status_code(interaction.guild_id, code, label, availability.value, interaction.user.id)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"✅ Status `{code.upper()}` — {E.safe(label)} ({availability.name}).")

    @departments.command(name="status-remove", description="Remove a unit status code")
    @app_commands.autocomplete(code=_status_autocomplete)
    async def status_remove(self, interaction: discord.Interaction, code: str):
        if not await require(interaction, "admin.config"):
            return
        try:
            ok = await self.bot.core.units.remove_status_code(interaction.guild_id, code, interaction.user.id)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, "Status removed." if ok else "No such status.")

    # ================================================================== /permissions
    permissions = app_commands.Group(name="permissions", description="Who can use what (role → permission groups)", guild_only=True)

    @permissions.command(name="view", description="Show role mappings and permission overrides")
    async def permissions_view(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.permissions"):
            return
        pm = self.bot.permissions
        roles = await pm.role_map(interaction.guild_id)
        ov = await pm.overrides(interaction.guild_id)
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🔐 Permissions", look=look, description=(
            "Discord **Administrator / Manage Server** members are always admins. Everyone counts as a *Citizen*."))
        e.add_field(name="Role mappings", value="\n".join(
            f"**{GROUP_LABEL[g]}**: " + (", ".join(f"<@&{r}>" for r in roles.get(g, [])) or "—") for g in GROUPS
            if g not in {"owner", "citizen"})[:1024], inline=False)
        e.add_field(name="Overrides", value="\n".join(f"`{p}` → {', '.join(gs) or 'nobody'}" for p, gs in ov.items())[:1024]
                    or "None (defaults apply)", inline=False)
        await reply(interaction, embed=e)

    @permissions.command(name="map", description="Add or remove a Discord role from a permission group")
    @app_commands.choices(group=GROUP_CHOICES)
    async def permissions_map(self, interaction: discord.Interaction, group: app_commands.Choice[str], role: discord.Role,
                              remove: bool = False):
        if not await require(interaction, "admin.permissions"):
            return
        if role.is_default():
            return await reply(interaction, "@everyone is already the Citizen group.", error=True)
        pm = self.bot.permissions
        current = set((await pm.role_map(interaction.guild_id)).get(group.value, []))
        current = current - {role.id} if remove else current | {role.id}
        await pm.set_group_roles(interaction.guild_id, group.value, current)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "permissions.map", group.value,
                                    {"role": role.name, "removed": remove})
        await self.bot.apply_private_channel_permissions(interaction.guild)
        await reply(interaction, f"{'➖' if remove else '➕'} {role.mention} {'removed from' if remove else 'added to'} **{group.name}**.")

    @permissions.command(name="set", description="Choose which groups may use a permission (empty = reset to default)")
    @app_commands.autocomplete(permission=_permission_autocomplete)
    @app_commands.describe(groups="Comma-separated: admin, supervisor, dispatcher, officer, fire_ems, citizen")
    async def permissions_set(self, interaction: discord.Interaction, permission: str, groups: Optional[str] = None):
        if not await require(interaction, "admin.permissions"):
            return
        if permission not in DEFAULT_PERMISSIONS:
            return await reply(interaction, "Unknown permission — pick one from the list.", error=True)
        if permission.startswith("admin.") and groups and "citizen" in groups:
            return await reply(interaction, "Administrator permissions can't be given to everyone.", error=True)
        chosen = None if not groups else [g.strip() for g in groups.split(",") if g.strip() in GROUPS]
        await self.bot.permissions.set_override(interaction.guild_id, permission, chosen)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "permissions.set", permission, {"groups": chosen})
        await reply(interaction, f"`{permission}` → {', '.join(chosen) if chosen else 'default groups'}.")

    # ================================================================== /reset-settings
    @app_commands.command(name="reset-settings", description="Reset this server's Pulse Dispatch settings (data is kept)")
    @app_commands.guild_only()
    @app_commands.choices(scope=[app_commands.Choice(name="Dispatch behaviour", value="dispatch"),
                                 app_commands.Choice(name="Logging", value="logging"),
                                 app_commands.Choice(name="Appearance & customization", value="appearance"),
                                 app_commands.Choice(name="AI mode / failover / models", value="ai"),
                                 app_commands.Choice(name="Everything above", value="all")])
    async def reset_settings(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        if not await require(interaction, "admin.config"):
            return
        bot, gid = self.bot, interaction.guild_id

        async def do_reset(i: discord.Interaction):
            done = []
            if scope.value in {"dispatch", "all"}:
                await bot.db.delete_config(gid, "dispatch_settings")
                done.append("dispatch behaviour")
            if scope.value in {"logging", "all"}:
                await bot.db.delete_config(gid, "log_settings")
                done.append("logging (all events on)")
            if scope.value in {"appearance", "all"}:
                for key in ("embed_settings", "callsign_format", "custom_categories"):
                    await bot.db.delete_config(gid, key)
                done.append("appearance & customization")
            if scope.value in {"ai", "all"}:
                await bot.db.delete_config(gid, "ai_settings")
                await bot.db.execute("UPDATE ai_providers SET model = NULL, enabled = 1, cooldown_until = NULL WHERE guild_id = ?", (gid,))
                bot.ai.clear_cache()
                done.append("AI mode/models (stored keys are kept)")
            await bot.audit.record(gid, i.user.id, "settings.reset", scope.value)
            await bot.bus.publish("config.changed", gid, what="reset")
            await i.response.edit_message(content="♻️ Reset: " + ", ".join(done) + ". Incidents, units and reports were not touched.",
                                          view=None)

        await reply(interaction, f"Reset **{scope.name}** to defaults for this server? Incidents, units, reports and keys are kept.",
                    view=ConfirmView(interaction.user.id, do_reset, confirm_label="Reset"))


async def setup(bot: "DispatchBot"):
    await bot.add_cog(PulseAdminCog(bot))
