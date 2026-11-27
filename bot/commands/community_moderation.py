"""v4.0 moderation commands: /warn /timeout /kick /ban /unban /history /case /report-member."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import reply, require
from community.common import as_int, fmt_duration
from community.moderation import ACTION_COLOUR, ACTION_ICON, ACTIONS
from database.models import discord_ts

from .community_ui import ConfirmView

if TYPE_CHECKING:
    from bot.client import DispatchBot


def case_embed(c: dict, look: E.Look) -> discord.Embed:
    e = E.base(f"{ACTION_ICON[c['action']]} Case #{c['number']} · {ACTIONS[c['action']]}", look=look,
               colour=E.MUTED if c["status"] != "active" else ACTION_COLOUR[c["action"]])
    e.add_field(name="Member", value=f"<@{c['target_id']}> ({E.safe(c.get('target_name') or c['target_id'], 40)})", inline=True)
    e.add_field(name="Moderator", value=f"<@{c['moderator_id']}>", inline=True)
    e.add_field(name="Status", value=c["status"].title(), inline=True)
    if c.get("duration_s"):
        e.add_field(name="Duration", value=fmt_duration(c["duration_s"]), inline=True)
    e.add_field(name="Created", value=discord_ts(c["created_at"], "f"), inline=True)
    e.add_field(name="Source", value=c.get("source", "discord").title(), inline=True)
    e.add_field(name="Reason", value=E.safe(c.get("reason") or "—", 1000), inline=False)
    if c.get("evidence"):
        e.add_field(name="Evidence", value=E.safe(c["evidence"], 500), inline=False)
    if c.get("outcome"):
        e.add_field(name="Outcome", value=E.safe(c["outcome"], 500), inline=False)
    return e


class CommunityModerationCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    async def _do(self, interaction: discord.Interaction, action: str, user_id: int, reason: str, *, evidence: str = "",
                  duration: Optional[str] = None, delete_days: int = 0) -> None:
        case = await self.bot.community.moderation.create(interaction.guild_id, action, user_id, interaction.user.id, reason=reason,
                                                           evidence=evidence, duration=duration, delete_message_days=delete_days)
        look = await E.look_for(self.bot, interaction.guild_id)
        embeds = [case_embed(case, look)]
        esc = case.get("escalation")
        if esc:
            if esc.get("error"):
                embeds[0].add_field(name="Automatic escalation failed", value=E.safe(esc["error"], 300), inline=False)
            else:
                embeds.append(case_embed(esc, look))
        await reply(interaction, embeds=embeds)

    @app_commands.command(name="warn", description="Warn a member (creates a moderation case)")
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 500],
                   evidence: Optional[str] = None):
        if await require(interaction, "mod.warn"):
            await self._do(interaction, "warn", member.id, reason, evidence=evidence or "")

    @app_commands.command(name="timeout", description="Time out a member (e.g. 10m, 2h, 1d - max 28d)")
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: app_commands.Range[str, 1, 500], evidence: Optional[str] = None):
        if await require(interaction, "mod.timeout"):
            await self._do(interaction, "timeout", member.id, reason, evidence=evidence or "", duration=duration)

    @app_commands.command(name="kick", description="Kick a member (asks for confirmation)")
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 500],
                   evidence: Optional[str] = None):
        if not await require(interaction, "mod.kick"):
            return
        view = ConfirmView(interaction.user.id, lambda i: self._do(i, "kick", member.id, reason, evidence=evidence or ""), label="Kick")
        await reply(interaction, f"👢 Kick {member.mention}?\n**Reason:** {E.safe(reason, 300)}", view=view)

    @app_commands.command(name="ban", description="Ban a user, even one who already left (asks for confirmation)")
    @app_commands.describe(user="Member or user ID", delete_message_days="Delete their messages from the last N days (0-7)")
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 1, 500],
                  delete_message_days: app_commands.Range[int, 0, 7] = 0, evidence: Optional[str] = None):
        if not await require(interaction, "mod.ban"):
            return
        view = ConfirmView(interaction.user.id, lambda i: self._do(i, "ban", user.id, reason, evidence=evidence or "",
                                                                   delete_days=delete_message_days), label="Ban")
        await reply(interaction, f"🔨 Ban {user.mention} (`{user.id}`)?\n**Reason:** {E.safe(reason, 300)}", view=view)

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: app_commands.Range[str, 1, 500]):
        if await require(interaction, "mod.ban"):
            await self._do(interaction, "unban", as_int(user_id, "User ID"), reason)

    @app_commands.command(name="history", description="A member's moderation history")
    @app_commands.guild_only()
    async def history(self, interaction: discord.Interaction, member: discord.User):
        if not await require(interaction, "mod.view"):
            return
        cases = await self.bot.community.moderation.history(interaction.guild_id, member.id)
        look = await E.look_for(self.bot, interaction.guild_id)
        active = sum(1 for c in cases if c["status"] == "active")
        lines = [f"{ACTION_ICON[c['action']]} **#{c['number']}** {ACTIONS[c['action']]}"
                 f"{' (' + fmt_duration(c['duration_s']) + ')' if c.get('duration_s') else ''} · {E.safe(c['reason'], 70)} · "
                 f"{discord_ts(c['created_at'], 'd')}{'' if c['status'] == 'active' else ' · *' + c['status'] + '*'}" for c in cases[:20]]
        e = E.base(f"📚 History · {member}", look=look, description="\n".join(lines) or "✨ Clean record.")
        e.set_footer(text=f"{len(cases)} case(s), {active} active • {look.footer()}")
        await reply(interaction, embed=e)

    case = app_commands.Group(name="case", description="Moderation cases", guild_only=True)

    @case.command(name="view", description="Show a moderation case")
    async def case_view(self, interaction: discord.Interaction, number: int):
        if not await require(interaction, "mod.view"):
            return
        c = await self.bot.community.moderation.get(interaction.guild_id, number)
        if not c:
            return await reply(interaction, "Case not found.", error=True)
        await reply(interaction, embed=case_embed(c, await E.look_for(self.bot, interaction.guild_id)))

    @case.command(name="revoke", description="Revoke a warning/timeout/kick case (lifts an active timeout)")
    async def case_revoke(self, interaction: discord.Interaction, number: int, reason: Optional[str] = None):
        if not await require(interaction, "mod.timeout"):
            return
        c = await self.bot.community.moderation.revoke(interaction.guild_id, number, interaction.user.id, reason or "")
        await reply(interaction, embed=case_embed(c, await E.look_for(self.bot, interaction.guild_id)))

    @case.command(name="edit", description="Update a case's reason or evidence")
    async def case_edit(self, interaction: discord.Interaction, number: int, reason: Optional[str] = None, evidence: Optional[str] = None):
        if not await require(interaction, "mod.warn"):
            return
        c = await self.bot.community.moderation.edit(interaction.guild_id, number, interaction.user.id, reason=reason, evidence=evidence)
        await reply(interaction, embed=case_embed(c, await E.look_for(self.bot, interaction.guild_id)))

    @app_commands.command(name="report-member", description="Privately report a member to the moderators")
    @app_commands.guild_only()
    async def report_member(self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 5, 1000],
                            evidence: Optional[str] = None):
        if not await require(interaction, "mod.report"):
            return
        r = await self.bot.community.moderation.report(interaction.guild_id, interaction.user.id, member.id, reason, evidence or "")
        await reply(interaction, f"✅ Report #{r['number']} sent to the moderators. Thank you — only staff can see it.")


async def setup(bot: "DispatchBot"):
    await bot.add_cog(CommunityModerationCog(bot))
