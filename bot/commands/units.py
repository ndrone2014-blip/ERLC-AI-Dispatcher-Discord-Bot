"""/unit and /status commands."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.commands import callsign_autocomplete, department_autocomplete, status_autocomplete
from bot.ui import BRAND, reply, require, truncate
from database.models import discord_ts
from dispatch.units import UnitError
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot


class UnitsCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    unit = app_commands.Group(name="unit", description="Manage your unit and duty status", guild_only=True)

    @unit.command(name="register", description="Register or update your officer profile and callsign")
    @app_commands.autocomplete(department=department_autocomplete)
    @app_commands.describe(callsign="Officers sharing a callsign are treated as one vehicle/unit")
    async def register(self, interaction: discord.Interaction, name: str, callsign: str, department: str,
                       roblox_username: Optional[str] = None):
        if not await require(interaction, "unit.register"):
            return
        try:
            o = await self.bot.core.units.register(interaction.guild_id, interaction.user.id, name, callsign, department, roblox_username)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"🪪 Registered **{o['name']}** as `{o['callsign']}` ({o['dept_name']}). Status: **{o['status_code']}**.")

    @unit.command(name="status", description="Set your unit status (e.g. 10-8, 10-7, 10-23)")
    @app_commands.autocomplete(code=status_autocomplete, callsign=callsign_autocomplete)
    async def status_cmd(self, interaction: discord.Interaction, code: str, callsign: Optional[str] = None):
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
                return await reply(interaction, "Register first with `/unit register`.", error=True)
            unit = await self.bot.core.units.unit(officer["unit_id"])
            officer_id = officer["id"]
        if unit is None:
            return await reply(interaction, "Unit not found.", error=True)
        try:
            unit = await self.bot.core.assignments.set_unit_status(unit["id"], code, interaction.user.id, officer_id)
        except UnitError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"`{unit['callsign']}` is now **{unit['status_code']}** ({unit.get('status_label')}).")

    @unit.command(name="remove", description="Remove your registration (or another officer's, for staff)")
    async def remove(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        if member and member.id != interaction.user.id and not await require(interaction, "unit.manage_other"):
            return
        ok = await self.bot.core.units.deactivate_officer(interaction.guild_id, target.id, interaction.user.id)
        await reply(interaction, "Registration removed." if ok else "No registration found.")

    @app_commands.command(name="status", description="View the unit board, or one unit's status")
    @app_commands.guild_only()
    @app_commands.autocomplete(callsign=callsign_autocomplete)
    async def status(self, interaction: discord.Interaction, callsign: Optional[str] = None):
        if not await require(interaction, "incident.view"):
            return
        if callsign:
            u = await self.bot.core.units.unit_by_callsign(interaction.guild_id, callsign)
            if u is None:
                return await reply(interaction, "Unit not found.", error=True)
            members = await self.bot.core.units.unit_members(u["id"])
            inc = await self.bot.core.incidents.get(u["current_incident_id"]) if u.get("current_incident_id") else None
            return await reply(interaction, f"`{u['callsign']}` ({u.get('dept_name')}) — **{u['status_code']}** {u.get('status_label') or ''} "
                                            f"since {discord_ts(u['status_changed_at'])}\nMembers: {', '.join(m['name'] for m in members) or '—'}\n"
                                            f"Incident: {('#' + str(inc['number']) + ' ' + (inc.get('nature') or '')) if inc else 'none'}")
        units = await self.bot.core.units.list_units(interaction.guild_id)
        lines = [f"`{u['callsign']:<8}` {u['status_code']:<9} {u.get('dept_code') or '':<4} "
                 f"{('#' + str(u['incident_number'])) if u.get('incident_number') else ''} {truncate(u.get('members'), 30)}" for u in units]
        await reply(interaction, embed=discord.Embed(title="👮 Units", description="\n".join(lines) or "No units on duty.", colour=BRAND))


async def setup(bot: "DispatchBot"):
    await bot.add_cog(UnitsCog(bot))
