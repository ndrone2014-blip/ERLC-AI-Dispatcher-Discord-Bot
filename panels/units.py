"""Panel 3: Unit Management."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

from bot.ui import BRAND, FOOTER, bot_of, reply, require, truncate
from database.models import discord_ts
from dispatch.units import UnitError
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot

AVAIL_EMOJI = {"available": "🟢", "assigned": "🟠", "unavailable": "⚪", "off": "⚫"}


async def build(bot: "DispatchBot", guild_id: int):
    units = await bot.core.units.list_units(guild_id)
    codes = await bot.core.units.status_codes(guild_id)
    e = discord.Embed(title="👮 Unit Board", colour=BRAND)
    by_dep: dict[str, list[str]] = {}
    for u in units:
        a = "assigned" if u.get("current_incident_id") else (u.get("availability") or "unavailable")
        line = (f"{AVAIL_EMOJI.get(a, '⚪')} `{u['callsign']}` **{u['status_code']}** {u.get('status_label') or ''}"
                f"{(' → #' + str(u['incident_number'])) if u.get('incident_number') else ''} · {truncate(u.get('members'), 40)}"
                f" · {discord_ts(u['status_changed_at'])}")
        by_dep.setdefault(u.get("dept_name") or "No department", []).append(line)
    if not by_dep:
        e.description = "No units on duty. Officers can register with **Register / Update My Unit**."
    for dep, lines in by_dep.items():
        chunk = "\n".join(lines)
        e.add_field(name=dep, value=truncate(chunk, 1020), inline=False)
    e.add_field(name="Status codes", value=" · ".join(f"`{c['code']}` {c['label']}" for c in codes) or "None configured", inline=False)
    e.set_footer(text=FOOTER)
    return [e], UnitsView(codes)


class UnitsView(discord.ui.View):
    def __init__(self, codes: Optional[list[dict]] = None):
        super().__init__(timeout=None)
        opts = [discord.SelectOption(label=f"{c['code']} — {c['label']}", value=c["code"],
                                     emoji=AVAIL_EMOJI.get(c["availability"])) for c in (codes or [])[:25]]
        self.status.options = opts or [discord.SelectOption(label="10-8 — Available", value="10-8")]

    @discord.ui.select(custom_id="cad:units:status", placeholder="Set MY unit status…", row=0)
    async def status(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "unit.self_status"):
            return
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        if not officer:
            return await reply(interaction, "Register your unit first.")
        try:
            unit = await bot.core.assignments.set_unit_status(officer["unit_id"], select.values[0], interaction.user.id, officer["id"])
        except UnitError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"`{unit['callsign']}` is now **{unit['status_code']}** ({unit.get('status_label')}).")

    @discord.ui.button(label="Register / Update My Unit", emoji="🪪", style=discord.ButtonStyle.primary, custom_id="cad:units:register", row=1)
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "unit.register"):
            return
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        await interaction.response.send_modal(RegisterModal(officer, await bot.core.units.departments(interaction.guild_id)))

    @discord.ui.button(label="Manage Another Unit", emoji="🛠️", style=discord.ButtonStyle.secondary, custom_id="cad:units:manage", row=1)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "unit.manage_other"):
            return
        await interaction.response.send_modal(ManageUnitModal())

    @discord.ui.button(label="Go Off Duty", emoji="⚫", style=discord.ButtonStyle.danger, custom_id="cad:units:offduty", row=1)
    async def offduty(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        if not officer:
            return await reply(interaction, "You're not registered.")
        code = await bot.core.units.code_for_availability(interaction.guild_id, "off")
        if not code:
            return await reply(interaction, "No off-duty status code configured.", error=True)
        unit = await bot.core.assignments.set_unit_status(officer["unit_id"], code, interaction.user.id, officer["id"])
        await reply(interaction, f"`{unit['callsign']}` is now **{unit['status_code']}**. Your registration is kept for next shift.")


class RegisterModal(discord.ui.Modal, title="Register your unit"):
    def __init__(self, officer: Optional[dict], departments: list[dict]):
        super().__init__()
        codes = ", ".join(d["short_code"] for d in departments)
        self.name = discord.ui.TextInput(label="Officer / roleplay name", max_length=40,
                                         default=officer["name"] if officer else None)
        self.callsign = discord.ui.TextInput(label="Callsign (same callsign = same vehicle)", max_length=15,
                                             default=officer["callsign"] if officer and officer.get("callsign") else None)
        self.department = discord.ui.TextInput(label=truncate(f"Department code ({codes})", 45), max_length=8,
                                               default=officer["dept_code"] if officer and officer.get("dept_code") else None)
        self.roblox = discord.ui.TextInput(label="Roblox username (optional)", max_length=40, required=False,
                                           default=officer["roblox_username"] if officer and officer.get("roblox_username") else None)
        for i in (self.name, self.callsign, self.department, self.roblox):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            officer = await bot.core.units.register(interaction.guild_id, interaction.user.id, self.name.value, self.callsign.value,
                                                    self.department.value.strip(), self.roblox.value)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"🪪 Registered **{officer['name']}** as `{officer['callsign']}` ({officer['dept_name']}). "
                                 f"Current status **{officer['status_code']}** — set 10-8 when you're available.")


class ManageUnitModal(discord.ui.Modal, title="Change another unit's status"):
    callsign = discord.ui.TextInput(label="Callsign", max_length=15)
    code = discord.ui.TextInput(label="New status code (e.g. 10-8, 10-7, OOS)", max_length=12)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        unit = await bot.core.units.unit_by_callsign(interaction.guild_id, self.callsign.value)
        if unit is None:
            return await reply(interaction, "Unit not found.", error=True)
        try:
            unit = await bot.core.assignments.set_unit_status(unit["id"], self.code.value.strip().upper(), interaction.user.id)
        except UnitError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"`{unit['callsign']}` set to **{unit['status_code']}**.")
