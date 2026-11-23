"""/bolo and /report commands."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui import WARN, has_perm, reply, require, truncate
from database.models import discord_ts, dumps
from dispatch.reports import REPORT_STATUSES
from panels.citizen_services import ReportModal
from panels.reports import show_report
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot

ALLOWED_ATTACHMENT_TYPES = ("image/", "video/", "application/pdf", "text/plain")


class RecordsCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    bolo = app_commands.Group(name="bolo", description="Be On the Look Out alerts", guild_only=True)

    @bolo.command(name="create", description="Issue a BOLO")
    @app_commands.choices(kind=[app_commands.Choice(name=n.title(), value=n) for n in ("vehicle", "person", "other")])
    async def bolo_create(self, interaction: discord.Interaction, kind: app_commands.Choice[str], subject: str,
                          description: Optional[str] = None, plate: Optional[str] = None, last_seen: Optional[str] = None,
                          reason: Optional[str] = None):
        if not await require(interaction, "bolo.manage"):
            return
        try:
            b = await self.bot.core.bolos.create(interaction.guild_id, interaction.user.id, kind=kind.value, subject=subject,
                                                 description=description or "", plate=plate or "", last_seen=last_seen or "",
                                                 reason=reason or "")
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"📢 BOLO #{b['number']} issued.")

    @bolo.command(name="list", description="List active BOLOs")
    async def bolo_list(self, interaction: discord.Interaction):
        if not await require(interaction, "bolo.view"):
            return
        rows = await self.bot.core.bolos.active(interaction.guild_id)
        await reply(interaction, embed=discord.Embed(title="📢 Active BOLOs", colour=WARN, description="\n".join(
            f"**#{b['number']}** {b['kind']}: {truncate(b['subject'], 60)}{(' · ' + b['plate']) if b.get('plate') else ''} · "
            f"{discord_ts(b['created_at'])}" for b in rows) or "None."))

    @bolo.command(name="clear", description="Clear a BOLO")
    async def bolo_clear(self, interaction: discord.Interaction, number: int, reason: Optional[str] = None):
        if not await require(interaction, "bolo.manage"):
            return
        b = await self.bot.core.bolos.get_by_number(interaction.guild_id, number)
        if b is None or b["status"] != "active":
            return await reply(interaction, "No active BOLO with that number.", error=True)
        await self.bot.core.bolos.clear(b["id"], interaction.user.id, reason or "")
        await reply(interaction, f"✅ BOLO #{number} cleared.")

    report = app_commands.Group(name="report", description="Police reports", guild_only=True)

    @report.command(name="file", description="File a private police report")
    async def report_file(self, interaction: discord.Interaction):
        if await require(interaction, "report.submit"):
            await interaction.response.send_modal(ReportModal())

    @report.command(name="status", description="Check the status of a report")
    async def report_status(self, interaction: discord.Interaction, number: int):
        rep = await self.bot.core.reports.get_by_number(interaction.guild_id, number)
        if rep is None or (rep["reporter_id"] != interaction.user.id and not await has_perm(interaction, "report.view_all")):
            return await reply(interaction, "No report with that number was filed by you.", error=True)
        await reply(interaction, f"📄 Report #{number}: **{REPORT_STATUSES.get(rep['status'], rep['status'])}** "
                                 f"(updated {discord_ts(rep['updated_at'])}).")

    @report.command(name="view", description="View and manage a report (authorised personnel)")
    async def report_view(self, interaction: discord.Interaction, number: int):
        if not await require(interaction, "report.view_all"):
            return
        rep = await self.bot.core.reports.get_by_number(interaction.guild_id, number)
        if rep is None:
            return await reply(interaction, "Report not found.", error=True)
        await show_report(interaction, rep["id"])

    @report.command(name="attach", description="Attach a screenshot/file to a report you filed")
    async def report_attach(self, interaction: discord.Interaction, number: int, file: discord.Attachment):
        rep = await self.bot.core.reports.get_by_number(interaction.guild_id, number)
        if rep is None or (rep["reporter_id"] != interaction.user.id and not await has_perm(interaction, "report.manage")):
            return await reply(interaction, "No report with that number was filed by you.", error=True)
        if not (file.content_type or "").startswith(ALLOWED_ATTACHMENT_TYPES) or file.size > 25 * 1024 * 1024:
            return await reply(interaction, "Only images, videos, PDFs or text files up to 25 MB.", error=True)
        attachments = list(rep.get("attachments") or [])[:9]
        attachments.append(file.url)
        await self.bot.db.execute("UPDATE reports SET attachments = ? WHERE id = ?", (dumps(attachments), rep["id"]))
        await self.bot.core.reports._history(interaction.guild_id, rep["id"], interaction.user.id, "attachment", filename=file.filename)
        await reply(interaction, f"📎 Attached `{file.filename}` to report #{number}.")


async def setup(bot: "DispatchBot"):
    await bot.add_cog(RecordsCog(bot))
