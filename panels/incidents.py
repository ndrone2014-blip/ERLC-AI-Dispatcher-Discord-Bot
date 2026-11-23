"""Panel 2: Active Incidents + per-incident actions (persistent dynamic buttons)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord

from bot.ui import (BRAND, DANGER, FOOTER, bot_of, has_perm, incident_embed, priority_badge, rate_limited, reply,
                    require, truncate)
from database.models import PRIORITIES, PRIORITY_EMOJI, PRIORITY_LABEL, VERIFICATION_LABEL, discord_ts, loads
from dispatch.incidents import IncidentError
from dispatch.units import UnitError
from security.validation import ValidationError, clean_text

if TYPE_CHECKING:
    from bot.client import DispatchBot

MAX_LISTED = 12


async def build(bot: "DispatchBot", guild_id: int):
    active = await bot.core.incidents.list_active(guild_id)
    e = discord.Embed(title="🚨 Active Incidents", colour=BRAND)
    sync = bot.sync.state(guild_id)
    if sync.status != "connected":
        e.description = (f"⚠️ **Live ER:LC verification unavailable** ({sync.status}). "
                         "New in-game 911 calls cannot be detected until the API reconnects.")
    if not active:
        e.add_field(name="No active incidents", value="All clear. New in-game 911 calls appear here automatically.", inline=False)
    for inc in active[:MAX_LISTED]:
        units = await bot.core.incidents.assigned_units(inc["id"])
        deps = loads(inc.get("recommended_departments"), []) or []
        badge = VERIFICATION_LABEL.get(inc["verification"], "")
        value = (f"📍 {truncate(inc.get('location') or 'Location unavailable', 80)}\n"
                 f"{priority_badge(inc)} · {badge} · {inc['status'].title()}\n"
                 f"👮 {', '.join(u['callsign'] for u in units) or 'No units'} · Rec: {inc.get('recommended_units') or '?'}"
                 f"{(' · ' + ', '.join(deps)) if deps else ''}\n"
                 f"🕒 {discord_ts(inc['created_at'])}")
        e.add_field(name=f"#{inc['number']} — {truncate(inc.get('nature') or 'Nature not provided', 60)}", value=value, inline=False)
    if len(active) > MAX_LISTED:
        e.add_field(name="…", value=f"{len(active) - MAX_LISTED} more incident(s). Use `/calls` to see all.", inline=False)
    backups = await bot.core.assignments.open_backup_requests(guild_id)
    if backups:
        e.add_field(name="🆘 Open backup requests",
                    value="\n".join(f"{b.get('callsign') or '?'} → #{b.get('incident_number') or '—'} ({b['urgency']})" for b in backups[:5]),
                    inline=False)
    e.set_footer(text=FOOTER)
    return [e], IncidentsPanelView(active[:25])


class IncidentsPanelView(discord.ui.View):
    def __init__(self, incidents: Optional[list[dict]] = None):
        super().__init__(timeout=None)
        incidents = incidents if incidents is not None else []
        options = [
            discord.SelectOption(label=truncate(f"#{i['number']} {i.get('nature') or 'Unknown'}", 95),
                                 description=truncate(i.get("location") or "Location unavailable", 95),
                                 value=str(i["id"]), emoji=PRIORITY_EMOJI.get(i.get("priority") or "", "⏳"))
            for i in incidents
        ] or [discord.SelectOption(label="No active incidents", value="none")]
        self.select.options = options
        self.select.disabled = options[0].value == "none"

    @discord.ui.select(custom_id="cad:incidents:select", placeholder="Select an incident to view or manage…", row=0)
    async def select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "incident.view"):
            return
        value = select.values[0] if select.values else "none"
        if value == "none":
            return await reply(interaction, "No active incidents.")
        await show_incident(interaction, int(value))

    @discord.ui.button(label="My Current Incident", emoji="🚓", style=discord.ButtonStyle.primary, custom_id="cad:incidents:mine", row=1)
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        if not officer:
            return await reply(interaction, "You are not registered. Use **Register / Update My Unit** on the unit board or `/unit register`.")
        if not officer.get("current_incident_id"):
            return await reply(interaction, f"`{officer['callsign']}` is not attached to an incident.")
        await show_incident(interaction, officer["current_incident_id"])

    @discord.ui.button(label="Report Incident (Unverified)", emoji="📝", style=discord.ButtonStyle.secondary,
                       custom_id="cad:incidents:manual", row=1)
    async def manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "incident.manual_create"):
            return
        await interaction.response.send_modal(ManualIncidentModal())

    @discord.ui.button(label="Backup Requests", emoji="🆘", style=discord.ButtonStyle.secondary, custom_id="cad:incidents:backups", row=1)
    async def backups(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "incident.view"):
            return
        bot = bot_of(interaction)
        rows = await bot.core.assignments.open_backup_requests(interaction.guild_id)
        if not rows:
            return await reply(interaction, "No open backup requests.")
        e = discord.Embed(title="🆘 Open Backup Requests", colour=DANGER)
        for b in rows[:10]:
            e.add_field(name=f"{b.get('callsign') or 'Unknown unit'} — {b['urgency'].title()}",
                        value=f"Incident #{b.get('incident_number') or '—'} · {discord_ts(b['created_at'])}\n{truncate(b.get('note'), 150)}",
                        inline=False)
        view = discord.ui.View(timeout=180)
        if await has_perm(interaction, "incident.attach_other"):
            for b in rows[:5]:
                view.add_item(IncidentAction("resolvebk", b["id"], label=f"Resolve {b.get('callsign') or b['id']}"))
        await reply(interaction, embed=e, view=view)


async def show_incident(interaction: discord.Interaction, incident_id: int, edit: bool = False):
    bot = bot_of(interaction)
    inc = await bot.core.incidents.get(incident_id)
    if inc is None or inc["guild_id"] != interaction.guild_id:
        return await reply(interaction, "Incident not found.", error=True)
    units = await bot.core.incidents.assigned_units(incident_id)
    notes = await bot.core.incidents.notes(incident_id)
    embed = incident_embed(inc, units, notes=notes)
    view = await incident_actions_view(interaction, inc)
    await reply(interaction, embed=embed, view=view)


async def incident_actions_view(interaction: discord.Interaction, inc: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if inc["status"] == "closed":
        view.add_item(IncidentAction("history", inc["id"]))
        return view
    staff = await has_perm(interaction, "incident.attach_other")
    sup = await has_perm(interaction, "incident.priority_approve")
    view.add_item(IncidentAction("attachme", inc["id"]))
    view.add_item(IncidentAction("detachme", inc["id"]))
    view.add_item(IncidentAction("backup", inc["id"]))
    view.add_item(IncidentAction("note", inc["id"]))
    view.add_item(IncidentAction("history", inc["id"]))
    if staff:
        view.add_item(IncidentAction("assign", inc["id"]))
        view.add_item(IncidentAction("unassign", inc["id"]))
        view.add_item(IncidentAction("update", inc["id"]))
        view.add_item(IncidentAction("reassess", inc["id"]))
        view.add_item(IncidentAction("close", inc["id"]))
    if sup:
        if inc.get("priority_status") == "recommended" and inc.get("ai_priority"):
            view.add_item(IncidentAction("approve", inc["id"]))
        view.add_item(IncidentAction("priority", inc["id"]))
        if inc["verification"] == "unverified":
            view.add_item(IncidentAction("review", inc["id"]))
    return view


ACTION_STYLE = {
    "attachme": ("Attach Me", "✅", discord.ButtonStyle.success),
    "detachme": ("Clear Me", "↩️", discord.ButtonStyle.secondary),
    "backup": ("Request Backup", "🆘", discord.ButtonStyle.danger),
    "note": ("Add Note / Update", "🗒️", discord.ButtonStyle.secondary),
    "history": ("History", "📜", discord.ButtonStyle.secondary),
    "assign": ("Attach Unit", "➕", discord.ButtonStyle.primary),
    "unassign": ("Detach Unit", "➖", discord.ButtonStyle.secondary),
    "update": ("Edit Details", "✏️", discord.ButtonStyle.secondary),
    "reassess": ("Reassess", "🧠", discord.ButtonStyle.secondary),
    "close": ("Close Incident", "🔒", discord.ButtonStyle.danger),
    "approve": ("Approve AI Priority", "🛡️", discord.ButtonStyle.success),
    "priority": ("Change Priority", "🎚️", discord.ButtonStyle.primary),
    "review": ("Supervisor Review", "🔍", discord.ButtonStyle.secondary),
    "view": ("View Incident", "📋", discord.ButtonStyle.primary),
    "resolvebk": ("Resolve Backup", "✔️", discord.ButtonStyle.success),
}


class IncidentAction(discord.ui.DynamicItem[discord.ui.Button], template=r"cad:i:(?P<action>[a-z]+):(?P<id>\d+)"):
    def __init__(self, action: str, target_id: int, label: Optional[str] = None):
        name, emoji, style = ACTION_STYLE.get(action, (action.title(), None, discord.ButtonStyle.secondary))
        super().__init__(discord.ui.Button(label=label or name, emoji=emoji, style=style,
                                           custom_id=f"cad:i:{action}:{target_id}"))
        self.action = action
        self.target_id = target_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        if await rate_limited(interaction):
            return
        if self.action == "resolvebk":
            if not await require(interaction, "incident.attach_other"):
                return
            await bot.core.assignments.resolve_backup(self.target_id, interaction.user.id)
            return await reply(interaction, "✔️ Backup request resolved.")
        inc = await bot.core.incidents.get(self.target_id)
        if inc is None or inc["guild_id"] != interaction.guild_id:
            return await reply(interaction, "Incident not found.", error=True)
        try:
            await self._run(interaction, bot, inc)
        except (IncidentError, UnitError, ValidationError) as e:
            await reply(interaction, f"⚠️ {e}", error=True)

    async def _run(self, interaction: discord.Interaction, bot: "DispatchBot", inc: dict):
        a = self.action
        if a == "view":
            if await require(interaction, "incident.view"):
                await show_incident(interaction, inc["id"])
            return
        if a == "history":
            if not await require(interaction, "incident.view"):
                return
            hist = await bot.core.incidents.history(inc["id"], 200)
            lines = [f"{discord_ts(h['at'], 't')} **{h['event_type']}**"
                     f"{(' by <@' + str(h['actor_id']) + '>') if h['actor_id'] else ''} — {truncate(_summarize(h), 140)}"
                     for h in hist[-20:]]
            e = discord.Embed(title=f"📜 Incident #{inc['number']} history", description="\n".join(lines) or "No history.", colour=BRAND)
            return await reply(interaction, embed=e)
        if a in {"attachme", "detachme", "backup"}:
            if not await require(interaction, "incident.attach_self" if a != "backup" else "incident.backup"):
                return
            officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
            if not officer or not officer.get("unit_id"):
                return await reply(interaction, "Register your unit first (`/unit register` or the unit board).")
            if a == "attachme":
                inc2, unit, prev = await bot.core.assignments.attach(inc["id"], officer["unit_id"], interaction.user.id)
                extra = f" (cleared from #{prev['number']})" if prev else ""
                return await reply(interaction, f"✅ `{unit['callsign']}` attached to incident #{inc2['number']}{extra}.")
            if a == "detachme":
                await bot.core.assignments.detach(inc["id"], officer["unit_id"], interaction.user.id)
                return await reply(interaction, f"↩️ `{officer['callsign']}` cleared from incident #{inc['number']}.")
            return await interaction.response.send_modal(BackupModal(inc["id"]))
        if a == "note":
            if await require(interaction, "incident.note"):
                await interaction.response.send_modal(NoteModal(inc["id"]))
            return
        if a in {"assign", "unassign"}:
            if not await require(interaction, "incident.attach_other"):
                return
            if a == "assign":
                units = [u for u in await bot.core.units.list_units(interaction.guild_id) if u.get("current_incident_id") != inc["id"]]
            else:
                units = await bot.core.incidents.assigned_units(inc["id"])
            if not units:
                return await reply(interaction, "No units to choose from.")
            return await reply(interaction, f"Choose a unit to {'attach to' if a == 'assign' else 'detach from'} #{inc['number']}:",
                               view=UnitPickView(inc["id"], units, attach=(a == "assign")))
        if a == "update":
            if await require(interaction, "incident.update"):
                await interaction.response.send_modal(UpdateIncidentModal(inc))
            return
        if a == "reassess":
            if not await require(interaction, "incident.update"):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            change = await bot.core.reassess(inc["id"], reason=f"Manual reassessment by {interaction.user.display_name}")
            new = await bot.core.incidents.get(inc["id"])
            msg = f"🧠 Reassessed #{new['number']}: {PRIORITY_LABEL.get(new.get('ai_priority') or '', '?')}, {new.get('recommended_units')} unit(s)."
            if change:
                msg += "\nSignificant change: " + "; ".join(change["reasons"])
            return await interaction.followup.send(msg, ephemeral=True)
        if a == "close":
            if await require(interaction, "incident.close"):
                await interaction.response.send_modal(CloseModal(inc["id"]))
            return
        if a == "approve":
            if not await require(interaction, "incident.priority_approve"):
                return
            new = await bot.core.incidents.set_priority(inc["id"], inc["ai_priority"], interaction.user.id, "Approved AI recommendation")
            await _priority_announcement(bot, new, inc.get("priority"))
            return await reply(interaction, f"🛡️ Approved {PRIORITY_LABEL[new['priority']]} priority for #{new['number']}.")
        if a == "priority":
            if await require(interaction, "incident.priority_approve"):
                await reply(interaction, f"Select the official priority for #{inc['number']} "
                                         f"(AI recommended: {PRIORITY_LABEL.get(inc.get('ai_priority') or '', 'none')}):",
                            view=PriorityPickView(inc["id"]))
            return
        if a == "review":
            if await require(interaction, "incident.review_unverified"):
                await reply(interaction, "Record your review of this **UNVERIFIED** report. "
                                         "(It stays unverified — only the ER:LC API can verify incidents.)", view=ReviewView(inc["id"]))
            return


def _summarize(h: dict) -> str:
    d = h.get("detail") or {}
    if h["event_type"] == "note":
        return d.get("note", "")
    if h["event_type"] == "assessment":
        a = d.get("assessment", {})
        return f"{a.get('priority')} · {a.get('recommended_units')} units · {', '.join(a.get('departments', []))} ({a.get('source')})"
    if h["event_type"] == "priority":
        return f"{d.get('old')} → {d.get('new')} ({d.get('status')}) {d.get('reason') or ''}"
    if h["event_type"] in {"unit_attached", "unit_detached"}:
        return d.get("unit", "")
    if h["event_type"] == "escalation":
        return "; ".join(d.get("reasons", []))
    return ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, "", [], {}))[:140]


async def _priority_announcement(bot: "DispatchBot", inc: dict, old: Optional[str]) -> None:
    if old == inc["priority"]:
        return
    await bot.core.announcements.enqueue(
        inc["guild_id"], "priority_change",
        f"Dispatch advises, incident {inc['number']} is now {PRIORITY_LABEL[inc['priority']]} priority.",
        dedupe_key=f"prio:{inc['id']}:{inc['priority']}:{inc['priority_approved_at']}", incident_id=inc["id"],
        incident_priority=inc["priority"])


class UnitPickView(discord.ui.View):
    def __init__(self, incident_id: int, units: list[dict], attach: bool):
        super().__init__(timeout=120)
        self.incident_id = incident_id
        self.attach = attach
        sel = discord.ui.Select(placeholder="Choose unit…", options=[
            discord.SelectOption(label=u["callsign"], value=str(u["id"]),
                                 description=truncate(f"{u.get('dept_code') or ''} · {u.get('status_code')} · {u.get('members') or ''}", 95))
            for u in units[:25]])
        sel.callback = self._picked
        self.add_item(sel)
        self.sel = sel

    async def _picked(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        if not await require(interaction, "incident.attach_other"):
            return
        unit_id = int(self.sel.values[0])
        try:
            if self.attach:
                inc, unit, _ = await bot.core.assignments.attach(self.incident_id, unit_id, interaction.user.id)
                msg = f"✅ `{unit['callsign']}` attached to #{inc['number']}."
            else:
                unit = await bot.core.assignments.detach(self.incident_id, unit_id, interaction.user.id)
                msg = f"➖ `{unit['callsign']}` detached."
        except (IncidentError, UnitError) as e:
            msg = f"⚠️ {e}"
        await interaction.response.edit_message(content=msg, view=None)


class PriorityPickView(discord.ui.View):
    def __init__(self, incident_id: int):
        super().__init__(timeout=120)
        self.incident_id = incident_id
        sel = discord.ui.Select(placeholder="Official priority…", options=[
            discord.SelectOption(label=PRIORITY_LABEL[p], value=p, emoji=PRIORITY_EMOJI[p]) for p in PRIORITIES])
        sel.callback = self._picked
        self.add_item(sel)
        self.sel = sel

    async def _picked(self, interaction: discord.Interaction):
        if not await require(interaction, "incident.priority_approve"):
            return
        await interaction.response.send_modal(PriorityReasonModal(self.incident_id, self.sel.values[0]))


class PriorityReasonModal(discord.ui.Modal, title="Priority decision"):
    reason = discord.ui.TextInput(label="Reason (recorded in incident history)", required=False, max_length=300,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, incident_id: int, priority: str):
        super().__init__()
        self.incident_id = incident_id
        self.priority = priority

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        old = await bot.core.incidents.get(self.incident_id)
        new = await bot.core.incidents.set_priority(self.incident_id, self.priority, interaction.user.id,
                                                    clean_text(self.reason.value, 300) or None)
        await _priority_announcement(bot, new, old.get("priority") if old else None)
        await reply(interaction, f"🎚️ #{new['number']} priority set to **{PRIORITY_LABEL[new['priority']]}** "
                                 f"({new['priority_status']}; AI recommended {PRIORITY_LABEL.get(new.get('ai_priority') or '', '—')}).")


class ReviewView(discord.ui.View):
    def __init__(self, incident_id: int):
        super().__init__(timeout=180)
        self.incident_id = incident_id

    async def _record(self, interaction: discord.Interaction, outcome: str):
        if not await require(interaction, "incident.review_unverified"):
            return
        await bot_of(interaction).core.incidents.mark_review(self.incident_id, interaction.user.id, outcome)
        await interaction.response.edit_message(content=f"🔍 Review recorded: **{outcome}** (incident remains UNVERIFIED).", view=None)

    @discord.ui.button(label="Plausible / allow response", style=discord.ButtonStyle.success)
    async def plausible(self, interaction, button):
        await self._record(interaction, "plausible")

    @discord.ui.button(label="Needs more information", style=discord.ButtonStyle.secondary)
    async def more(self, interaction, button):
        await self._record(interaction, "needs more information")

    @discord.ui.button(label="Not credible", style=discord.ButtonStyle.danger)
    async def bad(self, interaction, button):
        await self._record(interaction, "not credible")


class NoteModal(discord.ui.Modal, title="Incident note / update"):
    note = discord.ui.TextInput(label="New information", style=discord.TextStyle.paragraph, max_length=800)
    reassess = discord.ui.TextInput(label="Reassess with AI? (yes/no)", default="yes", max_length=3, required=False)

    def __init__(self, incident_id: int):
        super().__init__()
        self.incident_id = incident_id

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        author = officer["callsign"] if officer else interaction.user.display_name
        text = clean_text(self.note.value, 800, required=True, field="Note")
        await bot.core.incidents.add_note(self.incident_id, interaction.user.id, text, author=author)
        if (self.reassess.value or "yes").strip().lower().startswith("y"):
            await interaction.response.defer(ephemeral=True, thinking=True)
            change = await bot.core.reassess(self.incident_id, reason=f"Update from {author}")
            msg = "🗒️ Note added and incident reassessed."
            if change:
                msg += " Significant change: " + "; ".join(change["reasons"])
            return await interaction.followup.send(msg, ephemeral=True)
        await reply(interaction, "🗒️ Note added.")


class BackupModal(discord.ui.Modal, title="Request backup"):
    urgency = discord.ui.TextInput(label="Urgency: routine / urgent / emergency", default="routine", max_length=10)
    note = discord.ui.TextInput(label="Details (optional)", required=False, max_length=200)

    def __init__(self, incident_id: int):
        super().__init__()
        self.incident_id = incident_id

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        req = await bot.core.assignments.request_backup(interaction.guild_id, officer["unit_id"] if officer else None, self.incident_id,
                                                        interaction.user.id, self.urgency.value.strip().lower(),
                                                        clean_text(self.note.value, 200))
        await reply(interaction, "🆘 Backup request already open." if req.get("duplicate") else "🆘 Backup requested.")


class CloseModal(discord.ui.Modal, title="Close incident"):
    reason = discord.ui.TextInput(label="Disposition / reason", max_length=300, required=False, style=discord.TextStyle.paragraph)

    def __init__(self, incident_id: int):
        super().__init__()
        self.incident_id = incident_id

    async def on_submit(self, interaction: discord.Interaction):
        inc = await bot_of(interaction).core.incidents.close(self.incident_id, interaction.user.id, clean_text(self.reason.value, 300) or None)
        await reply(interaction, f"🔒 Incident #{inc['number']} closed.")


class UpdateIncidentModal(discord.ui.Modal, title="Edit incident details"):
    def __init__(self, inc: dict):
        super().__init__()
        self.incident_id = inc["id"]
        self.nature = discord.ui.TextInput(label="Nature", default=truncate(inc.get("nature") or "", 100), max_length=100, required=False)
        self.location = discord.ui.TextInput(label="Location", default=truncate(inc.get("location") or "", 150), max_length=150, required=False)
        self.description = discord.ui.TextInput(label="Description", default=truncate(inc.get("description") or "", 1000),
                                                style=discord.TextStyle.paragraph, max_length=1000, required=False)
        for item in (self.nature, self.location, self.description):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        await bot.core.incidents.update_fields(
            self.incident_id, interaction.user.id, nature=clean_text(self.nature.value, 100) or None,
            location=clean_text(self.location.value, 150) or None, description=clean_text(self.description.value, 1000) or None)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.core.reassess(self.incident_id, reason=f"Details edited by {interaction.user.display_name}")
        await interaction.followup.send("✏️ Incident updated and reassessed.", ephemeral=True)


class ManualIncidentModal(discord.ui.Modal, title="Report incident (UNVERIFIED)"):
    nature = discord.ui.TextInput(label="What is happening?", max_length=100)
    location = discord.ui.TextInput(label="Location", max_length=150)
    details = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, max_length=800, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        if not await bot.core.setting(interaction.guild_id, "allow_unverified_reports"):
            return await reply(interaction, "This server does not allow manual unverified incident reports.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        officer = await bot.core.units.officer_by_discord(interaction.guild_id, interaction.user.id)
        inc = await bot.core.create_manual_incident(
            interaction.guild_id, interaction.user.id, nature=clean_text(self.nature.value, 100, required=True, field="Nature"),
            location=clean_text(self.location.value, 150), description=clean_text(self.details.value, 800),
            reporter=officer["callsign"] if officer else interaction.user.display_name)
        await interaction.followup.send(f"📝 Created **UNVERIFIED** incident #{inc['number']}. Supervisors can review it.", ephemeral=True)
