"""Shared autocomplete helpers for slash commands."""
from __future__ import annotations

import discord
from discord import app_commands


async def incident_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot = interaction.client
    active = await bot.core.incidents.list_active(interaction.guild_id)
    cur = current.strip().lstrip("#")
    out = []
    for i in active:
        label = f"#{i['number']} {i.get('nature') or 'Unknown'} — {i.get('location') or 'no location'}"
        if not cur or cur in str(i["number"]) or cur.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=int(i["number"])))
    return out[:25]


async def status_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    codes = await interaction.client.core.units.status_codes(interaction.guild_id)
    return [app_commands.Choice(name=f"{c['code']} — {c['label']}"[:100], value=c["code"]) for c in codes
            if current.lower() in f"{c['code']} {c['label']}".lower()][:25]


async def department_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    deps = await interaction.client.core.units.departments(interaction.guild_id)
    return [app_commands.Choice(name=f"{d['short_code']} — {d['name']}"[:100], value=d["short_code"]) for d in deps
            if current.lower() in f"{d['short_code']} {d['name']}".lower()][:25]


async def callsign_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    units = await interaction.client.core.units.list_units(interaction.guild_id, include_off=True)
    return [app_commands.Choice(name=f"{u['callsign']} ({u.get('dept_code') or '—'}) {u['status_code']}"[:100], value=u["callsign"])
            for u in units if current.lower() in u["callsign"].lower()][:25]
