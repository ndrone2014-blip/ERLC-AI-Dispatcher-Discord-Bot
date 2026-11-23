"""Panel 10: Administration."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.ui import BRAND, FOOTER, bot_of, reply, require, truncate
from database.backups import create_backup, list_backups
from database.models import ANNOUNCEMENT_TYPES, PRIORITIES, discord_ts
from security.permissions import GROUP_LABEL, GROUPS
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot

SECTIONS = {
    "departments": ("Departments", "🏢"),
    "permissions": ("Roles & permissions", "🛡️"),
    "status": ("Status codes", "🔢"),
    "officers": ("Officers & callsigns", "👮"),
    "channels": ("Channels & panel locations", "📍"),
    "announcements": ("Announcement settings", "📣"),
    "dispatch": ("Dispatch settings", "⚙️"),
    "backups": ("Database backups", "💾"),
    "test": ("Test mode", "🧪"),
    "audit": ("Audit log", "📜"),
}


async def build(bot: "DispatchBot", guild_id: int):
    e = discord.Embed(title="🛠️ Administration", colour=BRAND, description="Select a section below. All changes are audit-logged.")
    deps = await bot.core.units.departments(guild_id)
    e.add_field(name="Departments", value=", ".join(f"`{d['short_code']}` {d['name']}" for d in deps) or "None", inline=False)
    roles = await bot.permissions.role_map(guild_id)
    e.add_field(name="Permission roles", value="\n".join(
        f"{GROUP_LABEL[g]}: {' '.join(f'<@&{r}>' for r in roles.get(g, [])) or '—'}" for g in GROUPS if g not in {"owner", "citizen"}),
        inline=False)
    toggles = await bot.core.announcements.toggles(guild_id)
    e.add_field(name="Announcements", value=" · ".join(f"{'✅' if v else '❌'} {ANNOUNCEMENT_TYPES[k]}" for k, v in toggles.items()),
                inline=False)
    log_ch = await bot.db.get_config(guild_id, "log_channel_id")
    e.add_field(name="Dispatch log channel", value=f"<#{log_ch}>" if log_ch else "Not set", inline=True)
    missing = await bot.panels.missing(guild_id)
    e.add_field(name="Panels", value="All deployed" if not missing else f"Missing: {', '.join(missing)}", inline=True)
    e.set_footer(text=FOOTER)
    return [e], AdminView()


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.section.options = [discord.SelectOption(label=label, value=key, emoji=emoji) for key, (label, emoji) in SECTIONS.items()]

    @discord.ui.select(custom_id="cad:admin:section", placeholder="Open an administration section…")
    async def section(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "admin.config"):
            return
        await open_section(interaction, select.values[0])


async def open_section(interaction: discord.Interaction, key: str):
    bot = bot_of(interaction)
    gid = interaction.guild_id
    if key == "departments":
        deps = await bot.core.units.departments(gid)
        e = discord.Embed(title="🏢 Departments", colour=BRAND, description="\n".join(
            f"`{d['short_code']}` **{d['name']}** · {d['kind']}" + (f" · <@&{d['role_id']}>" if d.get("role_id") else "") for d in deps))
        return await reply(interaction, embed=e, view=DepartmentsView())
    if key == "permissions":
        return await reply(interaction, "Choose a permission group, then the Discord roles that belong to it:", view=PermissionGroupView())
    if key == "status":
        codes = await bot.core.units.status_codes(gid)
        e = discord.Embed(title="🔢 Status codes", colour=BRAND, description="\n".join(
            f"`{c['code']}` {c['label']} — *{c['availability']}*" for c in codes))
        return await reply(interaction, embed=e, view=StatusCodesView())
    if key == "officers":
        rows = await bot.db.fetchall(
            "SELECT o.name, o.discord_id, u.callsign, d.short_code FROM officers o LEFT JOIN units u ON u.id = o.unit_id "
            "LEFT JOIN departments d ON d.id = o.department_id WHERE o.guild_id = ? AND o.active = 1 ORDER BY u.callsign", (gid,))
        e = discord.Embed(title="👮 Registered officers", colour=BRAND, description=truncate("\n".join(
            f"`{r['callsign'] or '—'}` {r['name']} ({r['short_code'] or '—'}) <@{r['discord_id']}>" for r in rows) or "None", 4000))
        return await reply(interaction, embed=e, view=OfficersView())
    if key == "channels":
        return await reply(interaction, "Set channels used by the dispatcher:", view=ChannelsView())
    if key == "announcements":
        return await reply(interaction, "Select the announcement types that should be **enabled** (unselected = disabled):",
                           view=AnnouncementToggleView(await bot.core.announcements.toggles(gid)))
    if key == "dispatch":
        return await interaction.response.send_modal(await DispatchSettingsModal.create(bot, gid))
    if key == "backups":
        files = list_backups(bot.settings.backup_dir)[:10]
        return await reply(interaction, "💾 Backups (newest first):\n" + ("\n".join(f"`{f.name}`" for f in files) or "None yet."),
                           view=BackupView())
    if key == "test":
        return await reply(interaction, "🧪 Simulated calls are labelled **TEST DATA** everywhere and are never treated as ER:LC calls.",
                           view=TestView())
    if key == "audit":
        rows = await bot.audit.recent(gid, 20)
        e = discord.Embed(title="📜 Audit log (latest 20)", colour=BRAND, description=truncate("\n".join(
            f"{discord_ts(r['at'], 'f')} <@{r['actor_id']}> `{r['action']}` {r.get('target') or ''}" for r in rows) or "Empty", 4000))
        return await reply(interaction, embed=e)


class DepartmentsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Add / Update Department", emoji="➕", style=discord.ButtonStyle.primary)
    async def add(self, interaction, button):
        if await require(interaction, "admin.config"):
            await interaction.response.send_modal(DepartmentModal())

    @discord.ui.button(label="Remove Department", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove(self, interaction, button):
        if await require(interaction, "admin.config"):
            await interaction.response.send_modal(RemoveDepartmentModal())


class DepartmentModal(discord.ui.Modal, title="Department"):
    name = discord.ui.TextInput(label="Name", max_length=60)
    code = discord.ui.TextInput(label="Short code (e.g. PD)", max_length=8)
    kind = discord.ui.TextInput(label="Type: law/fire/ems/supervisor/dispatch/other", max_length=12, default="law")
    role = discord.ui.TextInput(label="Discord role ID (optional)", max_length=20, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        role_id = int(self.role.value) if self.role.value.strip().isdigit() else None
        try:
            d = await bot.core.units.add_department(interaction.guild_id, self.name.value, self.code.value,
                                                    self.kind.value.strip().lower(), interaction.user.id, role_id)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"🏢 Saved `{d['short_code']}` {d['name']} ({d['kind']}).")


class RemoveDepartmentModal(discord.ui.Modal, title="Remove department"):
    code = discord.ui.TextInput(label="Short code", max_length=8)

    async def on_submit(self, interaction: discord.Interaction):
        ok = await bot_of(interaction).core.units.remove_department(interaction.guild_id, self.code.value.strip(), interaction.user.id)
        await reply(interaction, "Department removed (existing records keep their history)." if ok else "Department not found.")


class PermissionGroupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.group = None
        sel = discord.ui.Select(placeholder="Permission group…", options=[
            discord.SelectOption(label=GROUP_LABEL[g], value=g) for g in GROUPS if g not in {"owner", "citizen"}])
        sel.callback = self._group
        self.add_item(sel)
        self.sel = sel

    async def _group(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.permissions"):
            return
        group = self.sel.values[0]
        await interaction.response.edit_message(content=f"Select the roles for **{GROUP_LABEL[group]}** (replaces current roles):",
                                                view=RolePickView(group))


class RolePickView(discord.ui.View):
    def __init__(self, group: str):
        super().__init__(timeout=300)
        self.group = group
        sel = discord.ui.RoleSelect(placeholder="Roles…", min_values=0, max_values=10)
        sel.callback = self._roles
        self.add_item(sel)
        self.sel = sel

    async def _roles(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.permissions"):
            return
        bot = bot_of(interaction)
        await bot.permissions.set_group_roles(interaction.guild_id, self.group, [r.id for r in self.sel.values])
        await bot.audit.record(interaction.guild_id, interaction.user.id, "permissions.roles", self.group,
                               {"roles": [r.id for r in self.sel.values]})
        await bot.bus.publish("config.changed", interaction.guild_id, what="permissions")
        await bot.apply_private_channel_permissions(interaction.guild)
        await interaction.response.edit_message(content=f"✅ {GROUP_LABEL[self.group]}: "
                                                        f"{' '.join(r.mention for r in self.sel.values) or 'no roles'}", view=None)


class StatusCodesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Add / Update Code", emoji="➕", style=discord.ButtonStyle.primary)
    async def add(self, interaction, button):
        if await require(interaction, "admin.config"):
            await interaction.response.send_modal(StatusCodeModal())

    @discord.ui.button(label="Remove Code", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove(self, interaction, button):
        if await require(interaction, "admin.config"):
            await interaction.response.send_modal(RemoveStatusModal())


class StatusCodeModal(discord.ui.Modal, title="Status code"):
    code = discord.ui.TextInput(label="Code (e.g. 10-6)", max_length=12)
    label = discord.ui.TextInput(label="Label (e.g. Busy)", max_length=40)
    availability = discord.ui.TextInput(label="available / assigned / unavailable / off", max_length=12, default="unavailable")

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            await bot.core.units.save_status_code(interaction.guild_id, self.code.value, self.label.value,
                                                  self.availability.value.strip().lower(), interaction.user.id)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await bot.bus.publish("config.changed", interaction.guild_id, what="status_codes")
        await reply(interaction, "🔢 Status code saved.")


class RemoveStatusModal(discord.ui.Modal, title="Remove status code"):
    code = discord.ui.TextInput(label="Code", max_length=12)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            ok = await bot.core.units.remove_status_code(interaction.guild_id, self.code.value.strip(), interaction.user.id)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await bot.bus.publish("config.changed", interaction.guild_id, what="status_codes")
        await reply(interaction, "Removed." if ok else "Code not found.")


class OfficersView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        sel = discord.ui.UserSelect(placeholder="Remove an officer's registration…", max_values=1)
        sel.callback = self._remove
        self.add_item(sel)
        self.sel = sel

    async def _remove(self, interaction: discord.Interaction):
        if not await require(interaction, "unit.manage_other"):
            return
        user = self.sel.values[0]
        ok = await bot_of(interaction).core.units.deactivate_officer(interaction.guild_id, user.id, interaction.user.id)
        await reply(interaction, f"Removed {user.mention}'s registration." if ok else "That user is not registered.")


class ChannelsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Dispatch log channel…", channel_types=[discord.ChannelType.text], row=0)
    async def log_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._set(interaction, "log_channel_id", select.values[0])

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Department notifications / supervisor channel…",
                       channel_types=[discord.ChannelType.text], row=1)
    async def dept_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._set(interaction, "department_channel_id", select.values[0])

    async def _set(self, interaction: discord.Interaction, key: str, channel):
        if not await require(interaction, "admin.config"):
            return
        bot = bot_of(interaction)
        await bot.db.set_config(interaction.guild_id, key, channel.id)
        await bot.audit.record(interaction.guild_id, interaction.user.id, f"config.{key}", str(channel.id))
        await reply(interaction, f"Set to {channel.mention}.")

    @discord.ui.button(label="Repair Missing Panels", emoji="🧰", style=discord.ButtonStyle.primary, row=2)
    async def repair(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.config"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        restored = await bot_of(interaction).repair_panels(interaction.guild)
        await interaction.followup.send(f"🧰 Restored: {', '.join(restored) or 'nothing was missing'}.", ephemeral=True)

    @discord.ui.button(label="Move a Panel Here", emoji="📌", style=discord.ButtonStyle.secondary, row=2)
    async def move(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.config"):
            return
        from panels.base import PANEL_TITLES
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder="Panel to post in this channel…",
                                options=[discord.SelectOption(label=v, value=k) for k, v in PANEL_TITLES.items()])

        async def picked(i: discord.Interaction):
            if not await require(i, "admin.config"):
                return
            await bot_of(i).panels.post(i.guild_id, sel.values[0], i.channel)
            await i.response.edit_message(content=f"📌 {PANEL_TITLES[sel.values[0]]} posted here.", view=None)

        sel.callback = picked
        view.add_item(sel)
        await reply(interaction, "Which panel?", view=view)


class AnnouncementToggleView(discord.ui.View):
    def __init__(self, toggles: dict[str, bool]):
        super().__init__(timeout=300)
        sel = discord.ui.Select(min_values=0, max_values=len(ANNOUNCEMENT_TYPES), options=[
            discord.SelectOption(label=label, value=k, default=toggles.get(k, True)) for k, label in ANNOUNCEMENT_TYPES.items()])
        sel.callback = self._save
        self.add_item(sel)
        self.sel = sel

    async def _save(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.config"):
            return
        bot = bot_of(interaction)
        chosen = set(self.sel.values)
        for k in ANNOUNCEMENT_TYPES:
            await bot.core.announcements.set_toggle(interaction.guild_id, k, k in chosen)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "announcements.toggles", None, {"enabled": sorted(chosen)})
        await interaction.response.edit_message(content="📣 Announcement settings saved: " +
                                                        ", ".join(ANNOUNCEMENT_TYPES[k] for k in chosen) if chosen else "📣 All announcements disabled.",
                                                view=None)


class DispatchSettingsModal(discord.ui.Modal, title="Dispatch settings"):
    @classmethod
    async def create(cls, bot: "DispatchBot", guild_id: int) -> "DispatchSettingsModal":
        m = cls()
        s = {k: await bot.core.setting(guild_id, k) for k in
             ("priority_approval_threshold", "repeat_enabled", "repeat_interval_s", "repeat_max", "repeat_min_priority",
              "allow_unverified_reports")}
        m.threshold = discord.ui.TextInput(label="Supervisor approval needed from priority", default=s["priority_approval_threshold"],
                                           max_length=10)
        m.repeat = discord.ui.TextInput(label="Repeat unanswered calls? (yes/no)", default="yes" if s["repeat_enabled"] else "no",
                                        max_length=3)
        m.repeat_cfg = discord.ui.TextInput(label="Repeat interval seconds, max repeats", default=f"{s['repeat_interval_s']}, {s['repeat_max']}",
                                            max_length=20)
        m.repeat_min = discord.ui.TextInput(label="Only repeat calls of priority ≥", default=s["repeat_min_priority"], max_length=10)
        m.unverified = discord.ui.TextInput(label="Allow unverified officer reports? (yes/no)",
                                            default="yes" if s["allow_unverified_reports"] else "no", max_length=3)
        for i in (m.threshold, m.repeat, m.repeat_cfg, m.repeat_min, m.unverified):
            m.add_item(i)
        return m

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        gid = interaction.guild_id
        th = self.threshold.value.strip().lower()
        rm = self.repeat_min.value.strip().lower()
        if th not in PRIORITIES or rm not in PRIORITIES:
            return await reply(interaction, f"Priorities must be one of: {', '.join(PRIORITIES)}.", error=True)
        try:
            interval, max_r = [int(x.strip()) for x in self.repeat_cfg.value.split(",")]
        except ValueError:
            return await reply(interaction, "Repeat settings must look like `120, 2`.", error=True)
        await bot.core.set_setting(gid, "priority_approval_threshold", th)
        await bot.core.set_setting(gid, "repeat_enabled", self.repeat.value.strip().lower().startswith("y"))
        await bot.core.set_setting(gid, "repeat_interval_s", max(30, min(interval, 1800)))
        await bot.core.set_setting(gid, "repeat_max", max(0, min(max_r, 10)))
        await bot.core.set_setting(gid, "repeat_min_priority", rm)
        await bot.core.set_setting(gid, "allow_unverified_reports", self.unverified.value.strip().lower().startswith("y"))
        await bot.audit.record(gid, interaction.user.id, "config.dispatch_settings")
        await reply(interaction, "⚙️ Dispatch settings saved.")


class BackupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Create Backup Now", emoji="💾", style=discord.ButtonStyle.primary)
    async def backup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.backup"):
            return
        bot = bot_of(interaction)
        path = await create_backup(bot.db, bot.settings.backup_dir)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "db.backup", path.name)
        await reply(interaction, f"💾 Backup written: `{path.name}` (stored on the bot host in `data/backups`).")


class TestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Simulate 911 Call (TEST DATA)", emoji="🧪", style=discord.ButtonStyle.primary)
    async def call(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "admin.test"):
            await interaction.response.send_modal(TestCallModal())


class TestCallModal(discord.ui.Modal, title="Simulated 911 call — TEST DATA"):
    location = discord.ui.TextInput(label="Location", default="Test Location, Postal 000", max_length=150)
    description = discord.ui.TextInput(label="Description", default="Two-vehicle collision, one person injured", max_length=500,
                                       style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        inc = await bot.core.create_test_call(interaction.guild_id, interaction.user.id, location=self.location.value,
                                              description=self.description.value)
        await interaction.followup.send(f"🧪 Created TEST incident #{inc['number']} — it runs through the full assessment and "
                                        f"announcement pipeline and is labelled TEST DATA.", ephemeral=True)
