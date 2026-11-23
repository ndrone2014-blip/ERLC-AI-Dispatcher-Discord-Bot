"""Incident, call and dispatcher commands: /911 /calls /incident /attach /detach /backup /dispatch /test."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ai.conversation import Intent, parse_rules, parse_with_ai, strip_wake_word
from bot.commands import callsign_autocomplete, incident_autocomplete
from bot.ui import BRAND, has_perm, incident_embed, priority_badge, rate_limited, reply, require, truncate
from database.models import PRIORITIES, PRIORITY_LABEL, discord_ts
from dispatch.incidents import IncidentError
from dispatch.units import UnitError
from panels.citizen_services import Call911Modal
from panels.incidents import incident_actions_view
from security.validation import clean_text
from voice.commands import execute_intent

if TYPE_CHECKING:
    from bot.client import DispatchBot

PRIORITY_CHOICES = [app_commands.Choice(name=PRIORITY_LABEL[p], value=p) for p in PRIORITIES]


class DispatchCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    async def _incident(self, interaction: discord.Interaction, number: int) -> Optional[dict]:
        inc = await self.bot.core.incidents.get_by_number(interaction.guild_id, number)
        if inc is None:
            await reply(interaction, f"Incident #{number} not found.", error=True)
        return inc

    async def _my_unit(self, interaction: discord.Interaction) -> Optional[dict]:
        officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        if not officer or not officer.get("unit_id"):
            await reply(interaction, "You're not registered. Use `/unit register` first.", error=True)
            return None
        return officer

    # ------------------------------------------------------------------ /911 & /calls
    @app_commands.command(name="911", description="Submit a roleplay 911 call to dispatch")
    @app_commands.guild_only()
    async def nine_one_one(self, interaction: discord.Interaction):
        if await require(interaction, "call.submit") and not await rate_limited(interaction, "911"):
            await interaction.response.send_modal(Call911Modal())

    @app_commands.command(name="calls", description="View all active incidents")
    @app_commands.guild_only()
    async def calls(self, interaction: discord.Interaction):
        if not await require(interaction, "incident.view"):
            return
        active = await self.bot.core.incidents.list_active(interaction.guild_id)
        if not active:
            return await reply(interaction, "No active incidents.")
        lines = []
        for i in active[:30]:
            units = await self.bot.core.incidents.assigned_units(i["id"])
            lines.append(f"**#{i['number']}** {priority_badge(i)} · {truncate(i.get('nature') or 'Unknown', 50)} · "
                         f"{truncate(i.get('location') or 'no location', 40)} · {len(units)} unit(s) · {discord_ts(i['created_at'])}")
        await reply(interaction, embed=discord.Embed(title=f"🚨 Active incidents ({len(active)})", description="\n".join(lines), colour=BRAND))

    # ------------------------------------------------------------------ /incident
    incident = app_commands.Group(name="incident", description="View or manage incidents", guild_only=True)

    @incident.command(name="view", description="View an incident and its actions")
    @app_commands.autocomplete(number=incident_autocomplete)
    async def incident_view(self, interaction: discord.Interaction, number: int):
        if not await require(interaction, "incident.view"):
            return
        inc = await self._incident(interaction, number)
        if inc:
            units = await self.bot.core.incidents.assigned_units(inc["id"])
            notes = await self.bot.core.incidents.notes(inc["id"])
            await reply(interaction, embed=incident_embed(inc, units, notes=notes), view=await incident_actions_view(interaction, inc))

    @incident.command(name="create", description="Report an incident you witnessed (labelled UNVERIFIED)")
    async def incident_create(self, interaction: discord.Interaction, nature: str, location: str, details: Optional[str] = None):
        if not await require(interaction, "incident.manual_create"):
            return
        if not await self.bot.core.setting(interaction.guild_id, "allow_unverified_reports"):
            return await reply(interaction, "This server does not allow manual incident reports.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        inc = await self.bot.core.create_manual_incident(
            interaction.guild_id, interaction.user.id, nature=clean_text(nature, 100, required=True, field="Nature"),
            location=clean_text(location, 150), description=clean_text(details, 800),
            reporter=officer["callsign"] if officer else interaction.user.display_name)
        await interaction.followup.send(f"📝 Incident #{inc['number']} created and labelled **UNVERIFIED**.", ephemeral=True)

    @incident.command(name="note", description="Add new information to an incident (triggers reassessment)")
    @app_commands.autocomplete(number=incident_autocomplete)
    async def incident_note(self, interaction: discord.Interaction, number: int, text: str):
        if not await require(interaction, "incident.note"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.core.incidents.add_note(inc["id"], interaction.user.id, clean_text(text, 800, required=True, field="Note"),
                                               author=officer["callsign"] if officer else interaction.user.display_name)
        change = await self.bot.core.reassess(inc["id"], reason="New information added")
        await interaction.followup.send("🗒️ Note added." + (" Escalation: " + "; ".join(change["reasons"]) if change else ""), ephemeral=True)

    @incident.command(name="priority", description="Approve or override an incident's official priority (supervisors)")
    @app_commands.autocomplete(number=incident_autocomplete)
    @app_commands.choices(priority=PRIORITY_CHOICES)
    async def incident_priority(self, interaction: discord.Interaction, number: int, priority: app_commands.Choice[str],
                                reason: Optional[str] = None):
        if not await require(interaction, "incident.priority_approve"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        from panels.incidents import _priority_announcement
        new = await self.bot.core.incidents.set_priority(inc["id"], priority.value, interaction.user.id, clean_text(reason, 300) or None)
        await _priority_announcement(self.bot, new, inc.get("priority"))
        await reply(interaction, f"🎚️ #{new['number']} → **{priority.name}** ({new['priority_status']}; "
                                 f"AI recommended {PRIORITY_LABEL.get(new.get('ai_priority') or '', '—')}).")

    @incident.command(name="close", description="Close an incident")
    @app_commands.autocomplete(number=incident_autocomplete)
    async def incident_close(self, interaction: discord.Interaction, number: int, reason: Optional[str] = None):
        if not await require(interaction, "incident.close"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        try:
            await self.bot.core.incidents.close(inc["id"], interaction.user.id, clean_text(reason, 300) or None)
        except IncidentError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"🔒 Incident #{number} closed.")

    @incident.command(name="history", description="Show an incident's full history")
    async def incident_history(self, interaction: discord.Interaction, number: int):
        if not await require(interaction, "incident.view"):
            return
        inc = await self._incident(interaction, number)
        if not inc:
            return
        from panels.incidents import _summarize
        hist = await self.bot.core.incidents.history(inc["id"], 200)
        lines = [f"{discord_ts(h['at'], 'T')} **{h['event_type']}** — {truncate(_summarize(h), 150)}" for h in hist[-25:]]
        await reply(interaction, embed=discord.Embed(title=f"📜 Incident #{number}", description="\n".join(lines), colour=BRAND))

    # ------------------------------------------------------------------ attach / detach / backup
    @app_commands.command(name="attach", description="Attach yourself (or another unit) to an incident")
    @app_commands.guild_only()
    @app_commands.autocomplete(number=incident_autocomplete, callsign=callsign_autocomplete)
    async def attach(self, interaction: discord.Interaction, number: int, callsign: Optional[str] = None):
        if callsign:
            if not await require(interaction, "incident.attach_other"):
                return
            unit = await self.bot.core.units.unit_by_callsign(interaction.guild_id, callsign)
            if unit is None:
                return await reply(interaction, "Unit not found.", error=True)
            unit_id = unit["id"]
        else:
            if not await require(interaction, "incident.attach_self"):
                return
            officer = await self._my_unit(interaction)
            if not officer:
                return
            unit_id = officer["unit_id"]
        inc = await self._incident(interaction, number)
        if not inc:
            return
        try:
            inc, unit, prev = await self.bot.core.assignments.attach(inc["id"], unit_id, interaction.user.id)
        except (IncidentError, UnitError) as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"✅ `{unit['callsign']}` attached to #{inc['number']}" + (f" (cleared from #{prev['number']})" if prev else "") + ".")

    @app_commands.command(name="detach", description="Clear yourself (or another unit) from an incident")
    @app_commands.guild_only()
    @app_commands.autocomplete(number=incident_autocomplete, callsign=callsign_autocomplete)
    async def detach(self, interaction: discord.Interaction, number: Optional[int] = None, callsign: Optional[str] = None):
        if callsign:
            if not await require(interaction, "incident.attach_other"):
                return
            unit = await self.bot.core.units.unit_by_callsign(interaction.guild_id, callsign)
        else:
            if not await require(interaction, "incident.attach_self"):
                return
            officer = await self._my_unit(interaction)
            unit = await self.bot.core.units.unit(officer["unit_id"]) if officer else None
        if unit is None:
            return await reply(interaction, "Unit not found.", error=True)
        if number is None:
            if not unit.get("current_incident_id"):
                return await reply(interaction, f"`{unit['callsign']}` is not attached to an incident.")
            incident_id = unit["current_incident_id"]
        else:
            inc = await self._incident(interaction, number)
            if not inc:
                return
            incident_id = inc["id"]
        try:
            await self.bot.core.assignments.detach(incident_id, unit["id"], interaction.user.id)
        except (IncidentError, UnitError) as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"↩️ `{unit['callsign']}` cleared.")

    @app_commands.command(name="backup", description="Request backup")
    @app_commands.guild_only()
    @app_commands.choices(urgency=[app_commands.Choice(name=n.title(), value=n) for n in ("routine", "urgent", "emergency")])
    @app_commands.autocomplete(number=incident_autocomplete)
    async def backup(self, interaction: discord.Interaction, urgency: Optional[app_commands.Choice[str]] = None,
                     number: Optional[int] = None, note: Optional[str] = None):
        if not await require(interaction, "incident.backup"):
            return
        officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        incident_id = None
        if number is not None:
            inc = await self._incident(interaction, number)
            if not inc:
                return
            incident_id = inc["id"]
        req = await self.bot.core.assignments.request_backup(interaction.guild_id, officer["unit_id"] if officer else None, incident_id,
                                                             interaction.user.id, urgency.value if urgency else "routine",
                                                             clean_text(note, 200))
        await reply(interaction, "🆘 Backup request already open." if req.get("duplicate") else "🆘 Backup requested.")

    # ------------------------------------------------------------------ /dispatch
    dispatch = app_commands.Group(name="dispatch", description="Talk to or control the dispatcher", guild_only=True)

    @dispatch.command(name="ask", description="Send a natural-language request to dispatch, e.g. 'I'm 10-8' or 'what calls are active?'")
    async def dispatch_ask(self, interaction: discord.Interaction, message: str):
        if not await require(interaction, "voice.command"):
            return
        if await rate_limited(interaction, "ask"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, text = strip_wake_word(message)
        statuses = await self.bot.core.units.status_codes(interaction.guild_id)
        officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        intent = parse_rules(text, statuses)
        if intent is None and self.bot.ai is not None:
            ctx = self.bot.voice_commands.memory.get(interaction.guild_id, interaction.user.id)
            intent = await parse_with_ai(self.bot.ai, interaction.guild_id, text, statuses, ctx,
                                         officer.get("current_incident_id") if officer else None)
            if intent.name == "question":
                intent.args.setdefault("text", text)
        intent = intent or Intent("unclear", clarification="Dispatch, say again?")
        answer = await execute_intent(self.bot.core, interaction.guild_id, officer, intent, interaction.user.id, self.bot.voice_commands.memory)
        self.bot.voice_commands.memory.add(interaction.guild_id, interaction.user.id, "Officer", text)
        self.bot.voice_commands.memory.add(interaction.guild_id, interaction.user.id, "Dispatch", answer)
        await interaction.followup.send(f"📡 **Dispatch:** {answer}", ephemeral=True)

    @dispatch.command(name="notice", description="Broadcast a routine department notification (voice + log)")
    async def dispatch_notice(self, interaction: discord.Interaction, message: str):
        if not await require(interaction, "voice.control"):
            return
        ann = await self.bot.core.department_notice(interaction.guild_id, interaction.user.id, clean_text(message, 400, required=True,
                                                                                                          field="Message"))
        await reply(interaction, "📣 Notification queued." if ann else "Department notifications are disabled in Administration.")

    @dispatch.command(name="skip", description="Interrupt the announcement currently being spoken (supervisors)")
    async def dispatch_skip(self, interaction: discord.Interaction):
        if not await require(interaction, "announcement.skip_routine"):
            return
        ok, msg = await self.bot.player.skip_current(interaction.guild_id, allow_critical=await has_perm(interaction, "admin.config"))
        await reply(interaction, ("⏭️ " if ok else "⚠️ ") + msg)

    # ------------------------------------------------------------------ /test
    test = app_commands.Group(name="test", description="Test mode (simulated data is clearly labelled TEST DATA)", guild_only=True)

    @test.command(name="call", description="Simulate an in-game style 911 call through the full pipeline (TEST DATA)")
    async def test_call(self, interaction: discord.Interaction, description: str = "Two-vehicle collision, one person injured",
                        location: str = "Test Location, Postal 000"):
        if not await require(interaction, "admin.test"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        inc = await self.bot.core.create_test_call(interaction.guild_id, interaction.user.id, location=clean_text(location, 150),
                                                   description=clean_text(description, 500))
        await interaction.followup.send(f"🧪 TEST incident #{inc['number']} created: {priority_badge(inc)}, "
                                        f"{inc.get('recommended_units')} unit(s) recommended ({inc.get('assessment_source')}).",
                                        ephemeral=True)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(DispatchCog(bot))
