"""v4.0 staff commands: /staff, /shift, /session."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import has_perm, reply, require
from community.common import fmt_duration, parse_duration
from community.sessions import _ts
from database.models import discord_ts
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def _shift_type_ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    types = (await interaction.client.community.shifts.settings(interaction.guild_id))["types"]
    return [app_commands.Choice(name=t, value=t) for t in types if current.lower() in t.lower()][:25]


async def _rank_ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    ranks = (await interaction.client.community.staff.settings(interaction.guild_id))["ranks"]
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in ranks if current.lower() in r["name"].lower()][:25]


async def _session_ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    rows = await interaction.client.community.sessions.list(interaction.guild_id, "upcoming", 25)
    return [app_commands.Choice(name=f"#{s['number']} {s['name']} ({s['status']})"[:100], value=s["number"]) for s in rows
            if current.lower() in f"{s['number']} {s['name']}".lower()][:25]


def parse_when(text: Optional[str]) -> Optional[str]:
    """'in 30m', '2026-12-24 19:00' (UTC) -> ISO."""
    t = (text or "").strip()
    if not t:
        return None
    if t.lower().startswith("in "):
        secs = parse_duration(t[3:])
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M")
    try:
        return dt.datetime.fromisoformat(t.replace(" ", "T")).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValidationError("Use 'in 30m' / 'in 2h' or a UTC date and time like 2026-12-24 19:00.") from None


class CommunityStaffCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    # ------------------------------------------------------------------ staff
    staff = app_commands.Group(name="staff", description="Staff team management", guild_only=True)

    @staff.command(name="add", description="Add a member to the staff team")
    @app_commands.autocomplete(rank=_rank_ac)
    async def staff_add(self, interaction: discord.Interaction, member: discord.Member, rank: Optional[str] = None,
                        department: Optional[str] = None, reason: Optional[str] = None):
        if not await require(interaction, "staff.manage"):
            return
        r = await self.bot.community.staff.appoint(interaction.guild_id, member.id, rank or "", interaction.user.id,
                                                   department=department or "", reason=reason or "")
        msg = f"✅ {member.mention} joined the staff team as **{r['staff']['rank']}**."
        await reply(interaction, msg + ("\n⚠️ " + "\n⚠️ ".join(r["problems"]) if r["problems"] else ""))

    @staff.command(name="promote", description="Promote a staff member (next rank, or a specific rank)")
    @app_commands.autocomplete(rank=_rank_ac)
    async def staff_promote(self, interaction: discord.Interaction, member: discord.Member, rank: Optional[str] = None,
                            reason: Optional[str] = None):
        await self._rank(interaction, member, "up", rank, reason)

    @staff.command(name="demote", description="Demote a staff member (previous rank, or a specific rank)")
    @app_commands.autocomplete(rank=_rank_ac)
    async def staff_demote(self, interaction: discord.Interaction, member: discord.Member, rank: Optional[str] = None,
                           reason: Optional[str] = None):
        await self._rank(interaction, member, "down", rank, reason)

    async def _rank(self, interaction, member, direction, rank, reason):
        if not await require(interaction, "staff.manage"):
            return
        r = await self.bot.community.staff.change_rank(interaction.guild_id, member.id, interaction.user.id,
                                                       direction=None if rank else direction, rank=rank, reason=reason or "")
        msg = f"{'⬆️' if r['action'] == 'promoted' else '⬇️' if r['action'] == 'demoted' else '✏️'} {member.mention} is now **{r['staff']['rank']}**."
        await reply(interaction, msg + ("\n⚠️ " + "\n⚠️ ".join(r["problems"]) if r["problems"] else ""))

    @staff.command(name="remove", description="Remove a member from the staff team (asks for confirmation)")
    async def staff_remove(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        if not await require(interaction, "staff.manage"):
            return
        from .community_ui import ConfirmView

        async def go(i: discord.Interaction):
            await self.bot.community.staff.remove(i.guild_id, member.id, i.user.id, reason or "")
            await reply(i, f"🗑️ {member.mention} was removed from the staff team.")
        await reply(interaction, f"Remove {member.mention} from the staff team?", view=ConfirmView(interaction.user.id, go, label="Remove"))

    @staff.command(name="info", description="A staff member's profile")
    async def staff_info(self, interaction: discord.Interaction, member: discord.Member):
        if not await require(interaction, "staff.view"):
            return
        notes = await has_perm(interaction, "staff.notes")
        p = await self.bot.community.staff.profile(interaction.guild_id, member.id, include_notes=notes)
        st, sh = p["staff"], p["shifts"]
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"👥 {member.display_name} · {st['rank']}", look=look)
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="Status", value=st["status"].title(), inline=True)
        e.add_field(name="Department", value=E.safe(st.get("department") or "—", 40), inline=True)
        e.add_field(name="Appointed", value=discord_ts(st["appointed_at"], "D"), inline=True)
        e.add_field(name="Shifts", value=f"This week {sh['week_text']} · all time {sh['total_text']} ({sh['count']})", inline=False)
        e.add_field(name="History", value="\n".join(f"{discord_ts(h['at'], 'd')} {h['action']}"
                                                    f"{' → ' + h['to_rank'] if h.get('to_rank') and h['action'] in {'promoted', 'demoted', 'appointed'} else ''}"
                                                    for h in p["history"][:6]) or "—", inline=False)
        if p["moderation_actions"]:
            e.add_field(name="Recent moderation", value=f"{len(p['moderation_actions'])} recent case(s)", inline=True)
        if notes and p.get("notes"):
            e.add_field(name="🔒 Notes", value="\n".join(f"• {E.safe(n['note'], 120)}" for n in p["notes"][:4]), inline=False)
        await reply(interaction, embed=e)

    @staff.command(name="list", description="The staff directory")
    async def staff_list(self, interaction: discord.Interaction):
        if not await require(interaction, "staff.view"):
            return
        data = await self.bot.community.staff.list(interaction.guild_id, per_page=50)
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = [f"{'🟢' if it['on_shift'] else '🌙' if it['status'] == 'leave' else '⚪'} <@{it['user_id']}> · **{E.safe(it['rank'], 30)}**"
                 f"{' · ' + E.safe(it['department'], 30) if it.get('department') else ''}"
                 for it in data["items"]]
        e = E.base(f"👥 Staff Team ({data['total']})", look=look, description="\n".join(lines)[:4000] or "No staff yet. Add someone with `/staff add`.")
        e.set_footer(text="🟢 on shift · 🌙 on leave • " + look.footer())
        await reply(interaction, embed=e)

    @staff.command(name="note", description="Add a confidential note to a staff member's profile")
    async def staff_note(self, interaction: discord.Interaction, member: discord.Member, note: app_commands.Range[str, 1, 1000]):
        if not await require(interaction, "staff.notes"):
            return
        await self.bot.community.staff.add_note(interaction.guild_id, member.id, interaction.user.id, note)
        await reply(interaction, f"🔒 Note added to {member.mention}'s staff profile.")

    @staff.command(name="leave", description="Request leave from your staff duties")
    @app_commands.describe(start="First day, YYYY-MM-DD", end="Last day, YYYY-MM-DD")
    async def staff_leave(self, interaction: discord.Interaction, start: str, end: str, reason: app_commands.Range[str, 3, 500]):
        r = await self.bot.community.staff.request_leave(interaction.guild_id, interaction.user.id, start, end, reason)
        await reply(interaction, f"🌙 Leave request sent ({r['starts_on']} → {r['ends_on']}). You'll get a DM when it's reviewed.")

    # ------------------------------------------------------------------ shifts
    shift = app_commands.Group(name="shift", description="Staff shifts", guild_only=True)

    @shift.command(name="start", description="Start your shift")
    @app_commands.autocomplete(type=_shift_type_ac)
    async def shift_start(self, interaction: discord.Interaction, type: Optional[str] = None):
        if not await require(interaction, "shift.self"):
            return
        s = await self.bot.community.shifts.start(interaction.guild_id, interaction.user.id, type or "")
        await reply(interaction, f"🟢 **{s['shift_type']}** shift started {discord_ts(s['started_at'], 'R')}. End it with `/shift end`.")

    @shift.command(name="end", description="End your shift (or another member's, for supervisors)")
    async def shift_end(self, interaction: discord.Interaction, notes: Optional[str] = None, member: Optional[discord.Member] = None):
        target = member or interaction.user
        if target.id != interaction.user.id and not await require(interaction, "shift.manage"):
            return
        s = await self.bot.community.shifts.end(interaction.guild_id, target.id, interaction.user.id, notes or "")
        who = "Your" if target.id == interaction.user.id else f"{target.mention}'s"
        await reply(interaction, f"🔴 {who} {s['shift_type']} shift ended after **{fmt_duration(s['duration_s'])}**.")

    @shift.command(name="note", description="Add a note to your running shift")
    async def shift_note(self, interaction: discord.Interaction, note: app_commands.Range[str, 1, 300]):
        await self.bot.community.shifts.add_note(interaction.guild_id, interaction.user.id, note)
        await reply(interaction, "📝 Added to your shift notes.")

    @shift.command(name="status", description="Your shift status and totals")
    async def shift_status(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        if target.id != interaction.user.id and not await require(interaction, "staff.view"):
            return
        s = await self.bot.community.shifts.summary(interaction.guild_id, target.id)
        cur = s["current"]
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"⏱️ Shifts · {target.display_name}", look=look, colour=E.OK if cur else None)
        e.add_field(name="Now", value=f"🟢 {cur['shift_type']} since {discord_ts(cur['started_at'], 'R')}" if cur else "Off shift", inline=False)
        e.add_field(name="This week", value=s["week_text"], inline=True)
        e.add_field(name="All time", value=s["total_text"], inline=True)
        e.add_field(name="Shifts", value=str(s["count"]), inline=True)
        await reply(interaction, embed=e)

    @shift.command(name="active", description="Who is on shift right now")
    async def shift_active(self, interaction: discord.Interaction):
        if not await require(interaction, "shift.self"):
            return
        rows = await self.bot.community.shifts.active(interaction.guild_id)
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = [f"🟢 <@{r['user_id']}> · {E.safe(r['shift_type'], 20)} · {fmt_duration(r['elapsed_s'])}" for r in rows]
        await reply(interaction, embed=E.base(f"⏱️ On Shift ({len(rows)})", look=look, description="\n".join(lines) or "Nobody is on shift."))

    @shift.command(name="leaderboard", description="Most shift hours in the last 7 days")
    async def shift_leaderboard(self, interaction: discord.Interaction):
        if not await require(interaction, "staff.view"):
            return
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        rows = await self.bot.community.shifts.leaderboard(interaction.guild_id, since)
        look = await E.look_for(self.bot, interaction.guild_id)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> · **{r['hours']} h** ({r['shifts']} shifts)" for i, r in enumerate(rows)]
        await reply(interaction, embed=E.base("🏆 Shift Leaderboard · 7 days", look=look, description="\n".join(lines) or "No shifts yet."))

    # ------------------------------------------------------------------ sessions
    session = app_commands.Group(name="session", description="Community sessions", guild_only=True)

    @session.command(name="create", description="Schedule a session (announced in the session channel)")
    @app_commands.describe(start="'in 30m' or a UTC time like 2026-12-24 19:00 (empty = unscheduled)",
                           reminder_minutes="Ping this many minutes before the start (0 = no reminder)")
    async def session_create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 80], start: Optional[str] = None,
                             description: Optional[str] = None, host: Optional[discord.Member] = None, department: Optional[str] = None,
                             reminder_minutes: Optional[app_commands.Range[int, 0, 1440]] = None,
                             channel: Optional[discord.TextChannel] = None):
        if not await require(interaction, "session.manage"):
            return
        s = await self.bot.community.sessions.create(
            interaction.guild_id, interaction.user.id, name=name, description=description or "", scheduled_for=parse_when(start),
            host_id=host.id if host else None, department=department or "", reminder_minutes=reminder_minutes,
            channel_id=channel.id if channel else None)
        where = f" and announced in <#{s['channel_id']}>" if s.get("message_id") else \
            " (set an announcement channel on the dashboard or with `channel:` to announce it)"
        await reply(interaction, f"📅 Session **#{s['number']} {E.safe(s['name'], 60)}** created{where}. Start: {_ts(s['scheduled_for'])}")

    @session.command(name="start", description="Start a session (pings the session role)")
    @app_commands.autocomplete(number=_session_ac)
    async def session_start(self, interaction: discord.Interaction, number: int):
        if not await require(interaction, "session.manage"):
            return
        s = await self.bot.community.sessions.start(interaction.guild_id, number, interaction.user.id)
        await reply(interaction, f"🟢 Session **{E.safe(s['name'], 60)}** started.")

    @session.command(name="end", description="End a running session (or cancel a scheduled one)")
    @app_commands.autocomplete(number=_session_ac)
    async def session_end(self, interaction: discord.Interaction, number: int, notes: Optional[str] = None):
        if not await require(interaction, "session.manage"):
            return
        s = await self.bot.community.sessions.end(interaction.guild_id, number, interaction.user.id, notes or "")
        await reply(interaction, f"Session **{E.safe(s['name'], 60)}** {s['status']} · {s['participant_count']} participant(s).")

    @session.command(name="list", description="Upcoming and active sessions")
    async def session_list(self, interaction: discord.Interaction):
        rows = await self.bot.community.sessions.list(interaction.guild_id, "upcoming")
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = [f"{'🟢' if s['status'] == 'active' else '📅'} **#{s['number']} {E.safe(s['name'], 50)}** · "
                 f"{_ts(s.get('started_at') or s.get('scheduled_for'), 'R')} · {s['participant_count']} joined" for s in rows]
        await reply(interaction, embed=E.base("🎮 Sessions", look=look, description="\n".join(lines) or "No sessions scheduled."))

    @session.command(name="join", description="Sign up for a session")
    @app_commands.autocomplete(number=_session_ac)
    async def session_join(self, interaction: discord.Interaction, number: int):
        s = await self.bot.community.sessions.join(interaction.guild_id, number, interaction.user.id)
        await reply(interaction, f"🙋 You joined **{E.safe(s['name'], 60)}**. You'll get a DM reminder before it starts.")


async def setup(bot: "DispatchBot"):
    await bot.add_cog(CommunityStaffCog(bot))
