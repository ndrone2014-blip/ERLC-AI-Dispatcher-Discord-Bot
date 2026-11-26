"""Dispatch commands (Thanksgiving Update): /incident-view /incident-update /incident-close /incident-close-all /priority /broadcast."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.commands import incident_autocomplete
from bot.ui import incident_embed, reply, require
from database.models import PRIORITIES, PRIORITY_LABEL
from dispatch.incidents import IncidentError
from panels.incidents import incident_actions_view
from security.validation import clean_text

if TYPE_CHECKING:
    from bot.client import DispatchBot

PRIORITY_CHOICES = [app_commands.Choice(name=PRIORITY_LABEL[p], value=p) for p in PRIORITIES]


class ConfirmView(discord.ui.View):
    """Generic confirm / cancel prompt (only the person who ran the command can answer)."""

    def __init__(self, user_id: int, on_confirm, *, confirm_label: str = "Confirm", danger: bool = True):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.on_confirm = on_confirm
        self.confirm.label = confirm_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This prompt belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class PulseDispatchCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    async def _incident(self, interaction: discord.Interaction, number: int) -> Optional[dict]:
        inc = await self.bot.core.incidents.get_by_number(interaction.guild_id, number)
        if inc is None:
            await reply(interaction, f"Incident #{number} not found.", error=True)
        return inc

    @app_commands.command(name="incident-view", description="View an incident, its units and its actions")
    @app_commands.guild_only()
    @app_commands.autocomplete(number=incident_autocomplete)
    async def incident_view(self, interaction: discord.Interaction, number: int):
        if not await require(interaction, "incident.view"):
            return
        inc = await self._incident(interaction, number)
        if inc:
            units = await self.bot.core.incidents.assigned_units(inc["id"])
            notes = await self.bot.core.incidents.notes(inc["id"])
            await reply(interaction, embed=incident_embed(inc, units, notes=notes), view=await incident_actions_view(interaction, inc))

    @app_commands.command(name="incident-update", description="Update an incident's location, nature or details")
    @app_commands.guild_only()
    @app_commands.autocomplete(number=incident_autocomplete)
    @app_commands.describe(note="New information (added to the history and triggers a reassessment)")
    async def incident_update(self, interaction: discord.Interaction, number: int, location: Optional[str] = None,
                              nature: Optional[str] = None, note: Optional[str] = None):
        if not await require(interaction, "incident.update" if (location or nature) else "incident.note"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        if not (location or nature or note):
            return await reply(interaction, "Give at least one of: location, nature or note.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        changes = {}
        if location:
            changes["location"] = clean_text(location, 150, required=True, field="Location")
        if nature:
            changes["nature"] = clean_text(nature, 100, required=True, field="Nature")
        try:
            if changes:
                inc = await self.bot.core.incidents.update_fields(inc["id"], interaction.user.id, **changes)
            if note:
                officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
                await self.bot.core.incidents.add_note(inc["id"], interaction.user.id, clean_text(note, 800, required=True, field="Note"),
                                                       author=officer["callsign"] if officer else interaction.user.display_name)
        except IncidentError as e:
            return await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        change = await self.bot.core.reassess(inc["id"], reason="Incident updated")
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"✏️ Incident #{inc['number']} Updated", look=look)
        for k, v in changes.items():
            e.add_field(name=k.title(), value=E.safe(v, 200), inline=True)
        if note:
            e.add_field(name="Note", value=E.safe(note, 500), inline=False)
        if change:
            e.add_field(name="Reassessment", value=E.safe("; ".join(change["reasons"]), 500), inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="incident-close", description="Close an incident (asks for confirmation)")
    @app_commands.guild_only()
    @app_commands.autocomplete(number=incident_autocomplete)
    async def incident_close(self, interaction: discord.Interaction, number: int, reason: Optional[str] = None):
        if not await require(interaction, "incident.close"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        if inc["status"] == "closed":
            return await reply(interaction, f"Incident #{number} is already closed.", error=True)
        units = await self.bot.core.incidents.assigned_units(inc["id"])
        why = clean_text(reason, 300) or None

        async def do_close(i: discord.Interaction):
            try:
                await self.bot.core.incidents.close(inc["id"], i.user.id, why)
            except IncidentError as e:
                return await i.response.edit_message(content=f"⚠️ {e}", embed=None, view=None)
            await i.response.edit_message(content=f"🔒 Incident #{number} closed." + (f" {len(units)} unit(s) cleared." if units else ""),
                                          embed=None, view=None)

        await reply(interaction, f"Close incident **#{number}** ({E.safe(inc.get('nature') or 'Unknown', 60)} at "
                                 f"{E.safe(inc.get('location') or 'no location', 60)})?"
                    + (f" This clears {len(units)} assigned unit(s)." if units else ""),
                    view=ConfirmView(interaction.user.id, do_close, confirm_label="Close incident"))

    @app_commands.command(name="incident-close-all",
                          description="Close ALL active incidents and stop their repeat announcements (asks for confirmation)")
    @app_commands.guild_only()
    @app_commands.describe(reason="Why everything is being closed (saved in each incident's history)")
    async def incident_close_all(self, interaction: discord.Interaction, reason: Optional[str] = None):
        if not await require(interaction, "incident.close"):
            return
        active = await self.bot.core.incidents.list_active(interaction.guild_id, limit=1000)
        if not active:
            return await reply(interaction, "There are no active incidents to close.", error=True)
        why = clean_text(reason, 300) or "Bulk close (/incident-close-all)"

        async def do_close_all(i: discord.Interaction):
            await i.response.defer()
            closed = await self.bot.core.incidents.close_all(i.guild_id, i.user.id, why)
            nums = ", ".join(f"#{c['number']}" for c in closed[:25]) + (" …" if len(closed) > 25 else "")
            await i.edit_original_response(
                content=f"🔒 Closed **{len(closed)}** incident(s): {nums}\nQueued repeat announcements were cancelled.",
                embed=None, view=None)

        preview = ", ".join(f"#{inc['number']}" for inc in active[:25]) + (" …" if len(active) > 25 else "")
        await reply(interaction, f"Close **all {len(active)} active incident(s)**? ({preview})\n"
                                 "This clears every assigned unit and stops the bot repeating these calls.",
                    view=ConfirmView(interaction.user.id, do_close_all, confirm_label=f"Close all {len(active)}"))

    @app_commands.command(name="priority",description="Approve or change an incident's priority (supervisors)")
    @app_commands.guild_only()
    @app_commands.autocomplete(number=incident_autocomplete)
    @app_commands.choices(priority=PRIORITY_CHOICES)
    async def priority(self, interaction: discord.Interaction, number: int, priority: app_commands.Choice[str],
                       reason: Optional[str] = None):
        if not await require(interaction, "incident.priority_approve"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        from panels.incidents import _priority_announcement
        new = await self.bot.core.incidents.set_priority(inc["id"], priority.value, interaction.user.id, clean_text(reason, 300) or None)
        await _priority_announcement(self.bot, new, inc.get("priority"))
        await reply(interaction, f"🎚️ Incident #{new['number']} → **{priority.name}** ({new['priority_status']}; "
                                 f"AI recommended {PRIORITY_LABEL.get(new.get('ai_priority') or '', '—')}).")

    @app_commands.command(name="broadcast", description="Broadcast a message to all units (radio + dispatch channel + log)")
    @app_commands.guild_only()
    @app_commands.describe(message="What dispatch should say", urgency="Emergency traffic is spoken first and marked priority")
    @app_commands.choices(urgency=[app_commands.Choice(name="Routine", value="routine"),
                                   app_commands.Choice(name="Priority traffic", value="priority"),
                                   app_commands.Choice(name="Emergency traffic", value="emergency")])
    async def broadcast(self, interaction: discord.Interaction, message: str, urgency: Optional[app_commands.Choice[str]] = None):
        if not await require(interaction, "voice.control"):
            return
        text = clean_text(message, 400, required=True, field="Message")
        level = urgency.value if urgency else "routine"
        lead = {"emergency": "Emergency traffic. All units, ", "priority": "Priority traffic. All units, "}.get(level, "Dispatch to all units. ")
        spoken = lead + text
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "broadcast", level, {"text": text[:200]})
        from database.models import utcnow
        await self.bot.core.announcements.enqueue(
            interaction.guild_id, "department_notice", spoken, dedupe_key=f"broadcast:{interaction.user.id}:{utcnow().timestamp():.0f}",
            critical=level == "emergency", priority=95 if level == "emergency" else (65 if level == "priority" else None))
        look = await E.look_for(self.bot, interaction.guild_id)
        colour = {"emergency": E.DANGER, "priority": E.WARN}.get(level, look.accent)
        e = E.base({"emergency": "🚨 Emergency Traffic", "priority": "⚠️ Priority Traffic"}.get(level, "📣 Dispatch Broadcast"),
                   description=E.safe(text, 1500), colour=colour, look=look)
        e.add_field(name="From", value=interaction.user.mention, inline=True)
        ch = await self.bot.channel_for(interaction.guild_id, "dispatch_channel_id") or await self.bot.channel_for(
            interaction.guild_id, "department_channel_id")
        posted = False
        if ch is not None:
            try:
                await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
                posted = True
            except discord.HTTPException:
                pass
        await reply(interaction, "📣 Broadcast queued for the radio" + (f" and posted in {ch.mention}." if posted else "."))


async def setup(bot: "DispatchBot"):
    await bot.add_cog(PulseDispatchCog(bot))
