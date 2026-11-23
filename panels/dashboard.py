"""Panel 1: Main Dispatch Dashboard."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.ui import BRAND, FOOTER, bot_of, reply, require, status_dot, truncate
from database.models import discord_ts

if TYPE_CHECKING:
    from bot.client import DispatchBot

ERLC_LABEL = {
    "connected": "Connected", "error": "Error", "not_configured": "Not configured", "disabled": "Monitoring disabled",
    "rate_limited": "Rate limited", "starting": "Starting", "paused": "Paused",
}


async def build(bot: "DispatchBot", guild_id: int):
    core = bot.core
    active = await core.incidents.list_active(guild_id)
    pending = [i for i in active if i["status"] == "pending"]
    avail = await core.units.availability_summary(guild_id)
    sync = bot.sync.state(guild_id)
    state = await bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (guild_id,))
    vstatus = bot.voice.status(guild_id)
    vc = bot.voice.voice_client(guild_id)
    queued = await core.announcements.pending_count(guild_id)
    primary = await bot.ai.primary_name(guild_id) if bot.ai else None

    e = discord.Embed(title="📡 Dispatch Dashboard", colour=BRAND)
    e.add_field(name="Bot", value=f"🟢 Online since {discord_ts(bot.started_at_iso, 'R')}", inline=True)
    erlc_ok = sync.status == "connected"
    erlc_val = f"{status_dot(erlc_ok if sync.status != 'starting' else None)} {ERLC_LABEL.get(sync.status, sync.status)}"
    if state and state["server_name"]:
        erlc_val += f"\n**{truncate(state['server_name'], 40)}**"
    if state and state["current_players"] is not None and erlc_ok:
        erlc_val += f"\n👥 {state['current_players']}/{state['max_players']} players"
    if not erlc_ok:
        erlc_val += "\n⚠️ Live ER:LC verification unavailable"
    e.add_field(name="ER:LC API", value=erlc_val, inline=True)
    vtxt = {"disconnected": "🔴 Disconnected", "connected": "🟢 Connected", "listening": "🟢 Listening", "speaking": "🔊 Speaking"}[vstatus]
    if vc:
        vtxt += f"\n🔈 {vc.channel.name}"
    if queued:
        vtxt += f"\n🗂️ {queued} queued announcement(s)"
    e.add_field(name="Dispatcher Voice", value=vtxt, inline=True)
    e.add_field(name="Active Incidents", value=f"**{len(active)}**", inline=True)
    e.add_field(name="Pending (no units)", value=f"**{len(pending)}**", inline=True)
    e.add_field(name="AI", value=f"{status_dot(bool(primary))} {primary or 'Unavailable — rule-based fallback'}", inline=True)
    e.add_field(name="Units Available", value=f"🟢 **{avail['available']}**", inline=True)
    e.add_field(name="Units Assigned", value=f"🟠 **{avail['assigned']}**", inline=True)
    e.add_field(name="Unavailable / Off", value=f"⚪ {avail['unavailable']} / {avail['off']}", inline=True)
    deps = avail["by_department"]
    if deps:
        e.add_field(name="Department Activity", value="\n".join(
            f"`{code:<4}` 🟢 {d['available']}  🟠 {d['assigned']}  ⚪ {d['unavailable']}" for code, d in sorted(deps.items())),
            inline=False)
    feed = await bot.activity_feed(guild_id)
    e.add_field(name="Latest Activity", value="\n".join(f"{discord_ts(a['at'], 't')} {truncate(a['text'], 90)}" for a in feed[-6:])
                or "No activity yet.", inline=False)
    e.set_footer(text=FOOTER)
    return [e], DashboardView()


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _set(self, interaction: discord.Interaction, availability: str):
        if not await require(interaction, "unit.self_status"):
            return
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        if not officer:
            return await reply(interaction, "You're not registered yet — press **Register Unit**.")
        code = await bot.core.units.code_for_availability(interaction.guild_id, availability)
        if not code:
            return await reply(interaction, f"No status code is configured for '{availability}'.", error=True)
        unit = await bot.core.assignments.set_unit_status(officer["unit_id"], code, interaction.user.id, officer["id"])
        await reply(interaction, f"`{unit['callsign']}` is now **{unit['status_code']}** ({unit.get('status_label')}).")

    @discord.ui.button(label="Go Available (10-8)", emoji="🟢", style=discord.ButtonStyle.success, custom_id="cad:dash:avail")
    async def avail(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "available")

    @discord.ui.button(label="Go Unavailable (10-7)", emoji="⚪", style=discord.ButtonStyle.secondary, custom_id="cad:dash:unavail")
    async def unavail(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "unavailable")

    @discord.ui.button(label="Register Unit", emoji="🪪", style=discord.ButtonStyle.primary, custom_id="cad:dash:register")
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "unit.register"):
            return
        from panels.units import RegisterModal
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        deps = await bot.core.units.departments(interaction.guild_id)
        await interaction.response.send_modal(RegisterModal(officer, deps))

    @discord.ui.button(label="My Unit", emoji="👮", style=discord.ButtonStyle.secondary, custom_id="cad:dash:me")
    async def me(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        if not officer:
            return await reply(interaction, "You're not registered yet — press **Register Unit**.")
        inc = await bot.core.incidents.get(officer["current_incident_id"]) if officer.get("current_incident_id") else None
        await reply(interaction, f"👮 **{officer['name']}** · `{officer['callsign']}` · {officer.get('dept_name')}\n"
                                 f"Status: **{officer['status_code']}**\n"
                                 f"Incident: {('#' + str(inc['number']) + ' ' + (inc.get('nature') or '')) if inc else 'none'}")
