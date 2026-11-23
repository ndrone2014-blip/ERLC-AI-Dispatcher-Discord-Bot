"""Panel 6: Police Report Management (private channel, authorised personnel only)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord

from bot.ui import BRAND, FOOTER, bot_of, reply, require, truncate
from database.models import discord_ts
from dispatch.reports import REPORT_STATUSES, summarize_report
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def build(bot: "DispatchBot", guild_id: int):
    open_reports = await bot.core.reports.list(guild_id, status="open", limit=25)
    e = discord.Embed(title="📄 Police Reports", colour=BRAND,
                      description="Private — visible only to authorised personnel. Select a report to manage it.")
    for r in open_reports[:12]:
        e.add_field(name=f"#{r['number']} — {truncate(r.get('category') or 'Uncategorised', 50)}",
                    value=(f"{REPORT_STATUSES.get(r['status'], r['status'])} · {truncate(r.get('location'), 50) or '—'}\n"
                           f"Filed by <@{r['reporter_id']}> {discord_ts(r['created_at'])}"
                           + (f" · assigned <@{r['assigned_to']}>" if r.get("assigned_to") else "")), inline=False)
    if not open_reports:
        e.add_field(name="No open reports", value="New reports appear here automatically.", inline=False)
    e.set_footer(text=FOOTER)
    return [e], ReportsPanelView(open_reports)


class ReportsPanelView(discord.ui.View):
    def __init__(self, reports: Optional[list[dict]] = None):
        super().__init__(timeout=None)
        opts = [discord.SelectOption(label=truncate(f"#{r['number']} {r.get('category') or ''}", 95), value=str(r["id"]),
                                     description=truncate(REPORT_STATUSES.get(r["status"], r["status"]), 95)) for r in (reports or [])[:25]]
        self.select.options = opts or [discord.SelectOption(label="No open reports", value="none")]
        self.select.disabled = not opts

    @discord.ui.select(custom_id="cad:rep:select", placeholder="Select a report…")
    async def select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "report.view_all"):
            return
        if select.values[0] == "none":
            return await reply(interaction, "No open reports.")
        await show_report(interaction, int(select.values[0]))

    @discord.ui.button(label="Look Up by Number", emoji="🔎", custom_id="cad:rep:lookup")
    async def lookup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "report.view_all"):
            await interaction.response.send_modal(LookupModal())


async def show_report(interaction: discord.Interaction, report_id: int):
    bot = bot_of(interaction)
    rep = await bot.core.reports.get(report_id)
    if rep is None or rep["guild_id"] != interaction.guild_id:
        return await reply(interaction, "Report not found.", error=True)
    e = discord.Embed(title=f"📄 Report #{rep['number']} — {rep.get('category') or 'Uncategorised'}", colour=BRAND)
    e.add_field(name="Status", value=REPORT_STATUSES.get(rep["status"], rep["status"]), inline=True)
    e.add_field(name="Filed by", value=f"<@{rep['reporter_id']}> ({rep.get('rp_name') or '—'})", inline=True)
    e.add_field(name="Assigned", value=f"<@{rep['assigned_to']}>" if rep.get("assigned_to") else "Unassigned", inline=True)
    e.add_field(name="Location", value=rep.get("location") or "—", inline=True)
    e.add_field(name="When", value=rep.get("occurred_at") or "—", inline=True)
    e.add_field(name="Description (as reported)", value=truncate(rep["description"], 1000), inline=False)
    if rep.get("suspects"):
        e.add_field(name="Suspects (alleged, as reported)", value=truncate(rep["suspects"], 500), inline=False)
    if rep.get("witnesses"):
        e.add_field(name="Witnesses (as reported)", value=truncate(rep["witnesses"], 500), inline=False)
    if rep.get("attachments"):
        e.add_field(name="Attachments", value="\n".join(truncate(a, 200) for a in rep["attachments"][:5]), inline=False)
    if rep.get("summary"):
        e.add_field(name="AI summary (allegations not verified)", value=truncate(rep["summary"], 800), inline=False)
    view = discord.ui.View(timeout=None)
    for a in ("assignme", "status", "note", "supervisor", "summary", "history", "close"):
        view.add_item(ReportAction(a, rep["id"]))
    await reply(interaction, embed=e, view=view)


REPORT_ACTIONS = {
    "assignme": ("Assign to Me", "🙋", discord.ButtonStyle.primary),
    "status": ("Update Status", "🔄", discord.ButtonStyle.secondary),
    "note": ("Add Note", "🗒️", discord.ButtonStyle.secondary),
    "supervisor": ("Request Supervisor Review", "🛡️", discord.ButtonStyle.secondary),
    "summary": ("AI Summary", "🧠", discord.ButtonStyle.secondary),
    "history": ("History", "📜", discord.ButtonStyle.secondary),
    "close": ("Close Report", "🔒", discord.ButtonStyle.danger),
}


class ReportAction(discord.ui.DynamicItem[discord.ui.Button], template=r"cad:r:(?P<action>[a-z]+):(?P<id>\d+)"):
    def __init__(self, action: str, report_id: int):
        label, emoji, style = REPORT_ACTIONS[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"cad:r:{action}:{report_id}"))
        self.action = action
        self.report_id = report_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        rep = await bot.core.reports.get(self.report_id)
        if rep is None or rep["guild_id"] != interaction.guild_id:
            return await reply(interaction, "Report not found.", error=True)
        a = self.action
        if a == "history":
            if not await require(interaction, "report.view_all"):
                return
            hist = await bot.core.reports.history(rep["id"])
            lines = [f"{discord_ts(h['at'], 't')} **{h['event_type']}** by <@{h['actor_id']}> "
                     f"{truncate(', '.join(f'{k}: {v}' for k, v in h['detail'].items() if v), 150)}" for h in hist[-20:]]
            return await reply(interaction, embed=discord.Embed(title=f"📜 Report #{rep['number']} history",
                                                                description="\n".join(lines) or "None", colour=BRAND))
        if not await require(interaction, "report.manage" if a != "supervisor" else "report.view_all"):
            return
        if a == "assignme":
            await bot.core.reports.assign(rep["id"], interaction.user.id, interaction.user.id)
            return await reply(interaction, f"🙋 Report #{rep['number']} assigned to you.")
        if a == "status":
            return await reply(interaction, "Choose the new status:", view=ReportStatusView(rep["id"]))
        if a == "note":
            return await interaction.response.send_modal(ReportNoteModal(rep["id"]))
        if a == "supervisor":
            await bot.core.reports.set_status(rep["id"], interaction.user.id, "supervisor_review", "Supervisor review requested")
            await bot.notify_supervisors(interaction.guild_id, f"🛡️ Supervisor review requested for report #{rep['number']} "
                                                               f"by {interaction.user.mention}.")
            return await reply(interaction, "🛡️ Supervisor review requested.")
        if a == "summary":
            if bot.ai is None:
                return await reply(interaction, "No AI provider is configured.")
            await interaction.response.defer(ephemeral=True, thinking=True)
            summary = await summarize_report(bot.ai, interaction.guild_id, rep)
            if not summary:
                return await interaction.followup.send("AI is unavailable right now.", ephemeral=True)
            await bot.core.reports.set_summary(rep["id"], summary)
            return await interaction.followup.send(f"🧠 **AI summary (allegations not verified):**\n{summary}", ephemeral=True)
        if a == "close":
            await bot.core.reports.set_status(rep["id"], interaction.user.id, "closed")
            return await reply(interaction, f"🔒 Report #{rep['number']} closed.")


class ReportStatusView(discord.ui.View):
    def __init__(self, report_id: int):
        super().__init__(timeout=120)
        self.report_id = report_id
        sel = discord.ui.Select(options=[discord.SelectOption(label=v, value=k) for k, v in REPORT_STATUSES.items()])
        sel.callback = self._pick
        self.add_item(sel)
        self.sel = sel

    async def _pick(self, interaction: discord.Interaction):
        if not await require(interaction, "report.manage"):
            return
        rep = await bot_of(interaction).core.reports.set_status(self.report_id, interaction.user.id, self.sel.values[0])
        await interaction.response.edit_message(content=f"Report #{rep['number']} → **{REPORT_STATUSES[rep['status']]}**", view=None)


class ReportNoteModal(discord.ui.Modal, title="Report note"):
    note = discord.ui.TextInput(label="Note", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, report_id: int):
        super().__init__()
        self.report_id = report_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await bot_of(interaction).core.reports.add_note(self.report_id, interaction.user.id, self.note.value)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, "🗒️ Note added.")


class LookupModal(discord.ui.Modal, title="Find report"):
    number = discord.ui.TextInput(label="Report number", max_length=8)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.number.value.strip().lstrip("#"))
        except ValueError:
            return await reply(interaction, "Enter a number.", error=True)
        rep = await bot_of(interaction).core.reports.get_by_number(interaction.guild_id, n)
        if rep is None:
            return await reply(interaction, "Report not found.", error=True)
        await show_report(interaction, rep["id"])
