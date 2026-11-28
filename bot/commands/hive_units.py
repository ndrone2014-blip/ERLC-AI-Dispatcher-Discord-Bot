"""Unit commands (Thanksgiving Update):
/unit-register /unit-status /unit-assign /unit-unassign /unit-list /available-units."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.commands import callsign_autocomplete, department_autocomplete, incident_autocomplete, status_autocomplete
from bot.ui import reply, require
from database.models import DEPARTMENT_KINDS
from dispatch.incidents import IncidentError
from dispatch.units import UnitError
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot

AVAIL_ICON = {"available": "🟢", "assigned": "🔵", "unavailable": "🟡", "off": "⚫"}
KIND_CHOICES = [app_commands.Choice(name=v.split(" (")[0], value=k) for k, v in DEPARTMENT_KINDS.items() if k not in {"dispatch", "other"}]


async def _check_callsign(bot: "DispatchBot", guild_id: int, callsign: str, department: str) -> Optional[str]:
    """Server rule from /settings callsign-format. Returns an error message or None."""
    fmt = await bot.db.get_config(guild_id, "callsign_format", "any") or "any"
    cs = callsign.strip().upper()
    if fmt == "department_prefix" and not cs.startswith(department.strip().upper()):
        return f"On this server callsigns must start with the department code, e.g. `{department.upper()}-12`."
    if fmt == "numeric" and not cs.replace("-", "").isdigit():
        return "On this server callsigns must be numbers, e.g. `1-23` or `412`."
    return None


class HiveUnitsCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    @app_commands.command(name="unit-register", description="Register your unit: name, callsign and department")
    @app_commands.guild_only()
    @app_commands.autocomplete(department=department_autocomplete)
    @app_commands.describe(callsign="Your unit identifier (officers sharing a callsign are one unit/vehicle)")
    async def unit_register(self, interaction: discord.Interaction, name: str, callsign: str, department: str,
                            roblox_username: Optional[str] = None):
        if not await require(interaction, "unit.register"):
            return
        problem = await _check_callsign(self.bot, interaction.guild_id, callsign, department)
        if problem:
            return await reply(interaction, f"⚠️ {problem}", error=True)
        try:
            o = await self.bot.core.units.register(interaction.guild_id, interaction.user.id, name, callsign, department, roblox_username)
        except (ValidationError, UnitError) as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🪪 Unit Registered", look=look, colour=E.OK)
        e.add_field(name="Officer", value=E.safe(o["name"], 60), inline=True)
        e.add_field(name="Callsign", value=f"`{o['callsign']}`", inline=True)
        e.add_field(name="Department", value=E.safe(o["dept_name"], 60), inline=True)
        e.add_field(name="Status", value=f"**{o['status_code']}**", inline=True)
        e.add_field(name="Next", value="Go on duty with `/unit-status 10-8`. Talk to dispatch with `/dispatch ask`.", inline=False)
        await reply(interaction, embed=e)

    @app_commands.command(name="unit-status", description="Set your status (Available, En Route, On Scene, Busy, Out of Service…)")
    @app_commands.guild_only()
    @app_commands.autocomplete(code=status_autocomplete, callsign=callsign_autocomplete)
    @app_commands.describe(callsign="Staff only: set another unit's status")
    async def unit_status(self, interaction: discord.Interaction, code: str, callsign: Optional[str] = None):
        if callsign:
            if not await require(interaction, "unit.manage_other"):
                return
            unit = await self.bot.core.units.unit_by_callsign(interaction.guild_id, callsign)
            officer_id = None
        else:
            if not await require(interaction, "unit.self_status"):
                return
            officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
            if not officer:
                return await reply(interaction, "Register first with `/unit-register`.", error=True)
            unit = await self.bot.core.units.unit(officer["unit_id"])
            officer_id = officer["id"]
        if unit is None:
            return await reply(interaction, "Unit not found.", error=True)
        try:
            unit = await self.bot.core.assignments.set_unit_status(unit["id"], code, interaction.user.id, officer_id)
        except UnitError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        from dispatch.phrases import ack_status
        await reply(interaction, f"📻 {ack_status(unit['callsign'], unit['status_code'], unit.get('status_label') or '', interaction.guild_id)}")

    @app_commands.command(name="unit-assign", description="Assign a unit to an incident (reassigns it if it is on another call)")
    @app_commands.guild_only()
    @app_commands.autocomplete(callsign=callsign_autocomplete, number=incident_autocomplete)
    async def unit_assign(self, interaction: discord.Interaction, callsign: str, number: int):
        if not await require(interaction, "incident.attach_other"):
            return
        unit = await self.bot.core.units.unit_by_callsign(interaction.guild_id, callsign)
        inc = await self.bot.core.incidents.get_by_number(interaction.guild_id, number)
        if unit is None or inc is None:
            return await reply(interaction, "Unit or incident not found.", error=True)
        if unit.get("availability") == "off":
            return await reply(interaction, f"`{unit['callsign']}` is out of service. Set them on duty first.", error=True)
        try:
            inc, unit, prev = await self.bot.core.assignments.attach(inc["id"], unit["id"], interaction.user.id)
        except (IncidentError, UnitError) as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"🚓 `{unit['callsign']}` assigned to incident #{inc['number']}"
                                 + (f" (reassigned from #{prev['number']})" if prev else "") + ".")

    @app_commands.command(name="unit-unassign", description="Clear a unit from its incident")
    @app_commands.guild_only()
    @app_commands.autocomplete(callsign=callsign_autocomplete, number=incident_autocomplete)
    async def unit_unassign(self, interaction: discord.Interaction, callsign: str, number: Optional[int] = None):
        if not await require(interaction, "incident.attach_other"):
            return
        unit = await self.bot.core.units.unit_by_callsign(interaction.guild_id, callsign)
        if unit is None:
            return await reply(interaction, "Unit not found.", error=True)
        if number is not None:
            inc = await self.bot.core.incidents.get_by_number(interaction.guild_id, number)
            if inc is None:
                return await reply(interaction, f"Incident #{number} not found.", error=True)
            incident_id = inc["id"]
        else:
            incident_id = unit.get("current_incident_id")
            if not incident_id:
                return await reply(interaction, f"`{unit['callsign']}` is not assigned to an incident.")
        try:
            await self.bot.core.assignments.detach(incident_id, unit["id"], interaction.user.id)
        except (IncidentError, UnitError) as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"↩️ `{unit['callsign']}` cleared.")

    async def _unit_board(self, guild_id: int, units: list[dict], title: str) -> discord.Embed:
        look = await E.look_for(self.bot, guild_id)
        e = E.base(title, look=look)
        by_dept: dict[str, list[str]] = {}
        for u in units:
            line = (f"{AVAIL_ICON.get(u.get('availability') or '', '⚪')} `{u['callsign']}` **{u['status_code']}** "
                    f"{u.get('status_label') or ''}"
                    + (f" · #{u['incident_number']}" if u.get("incident_number") else "")
                    + (f" · {E.safe(u.get('members'), 40)}" if u.get("members") else ""))
            by_dept.setdefault(u.get("dept_name") or "No department", []).append(line)
        for dept, lines in list(by_dept.items())[:20]:
            e.add_field(name=f"{dept} ({len(lines)})", value="\n".join(lines)[:1024], inline=False)
        if not units:
            e.description = "No units match."
        return e

    @app_commands.command(name="unit-list", description="List units by department and status")
    @app_commands.guild_only()
    @app_commands.autocomplete(department=department_autocomplete)
    async def unit_list(self, interaction: discord.Interaction, department: Optional[str] = None, include_off_duty: bool = False):
        if not await require(interaction, "incident.view"):
            return
        units = await self.bot.core.units.list_units(interaction.guild_id, include_off=include_off_duty)
        if department:
            units = [u for u in units if (u.get("dept_code") or "").upper() == department.upper()]
        await reply(interaction, embed=await self._unit_board(interaction.guild_id, units, f"🚓 Units ({len(units)})"))

    @app_commands.command(name="available-units", description="Which units are available right now")
    @app_commands.guild_only()
    @app_commands.choices(kind=KIND_CHOICES)
    async def available_units(self, interaction: discord.Interaction, kind: Optional[app_commands.Choice[str]] = None):
        if not await require(interaction, "incident.view"):
            return
        units = [u for u in await self.bot.core.units.list_units(interaction.guild_id) if u.get("availability") == "available"]
        if kind:
            units = [u for u in units if u.get("dept_kind") == kind.value]
        await reply(interaction, embed=await self._unit_board(
            interaction.guild_id, units, f"🟢 Available Units ({len(units)})" + (f" · {kind.name}" if kind else "")))


async def setup(bot: "DispatchBot"):
    await bot.add_cog(HiveUnitsCog(bot))
