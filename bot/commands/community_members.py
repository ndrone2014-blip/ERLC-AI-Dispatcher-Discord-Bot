"""v4.0 member commands: /verify, /verification, /profile, /member."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import has_perm, reply, require
from database.models import discord_ts

from .community_ui import VerifyStartItem, start_verify, verification_panel_embed

if TYPE_CHECKING:
    from bot.client import DispatchBot


def _role(role_id) -> str:
    return f"<@&{role_id}>" if role_id else "none"


def _roblox_line(link: Optional[dict]) -> str:
    if not link:
        return "Not verified"
    return f"[{E.safe(link['roblox_username'], 40)}]({link['profile_url']}) · {link['method'].replace('_', ' ')} · {discord_ts(link['verified_at'], 'R')}"


class CommunityMembersCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    # ------------------------------------------------------------------ members
    @app_commands.command(name="verify", description="Link your Roblox account to your Discord account")
    @app_commands.describe(roblox_username="Your Roblox username (leave empty to choose a method)")
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction, roblox_username: Optional[str] = None):
        await start_verify(interaction, roblox_username)

    @app_commands.command(name="profile", description="Your community profile (or another member's)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        if target.id != interaction.user.id and not await require(interaction, "members.view"):
            return
        c = self.bot.community
        look = await E.look_for(self.bot, interaction.guild_id)
        link = await c.verification.get_link(interaction.guild_id, target.id)
        staff = await c.staff.get(interaction.guild_id, target.id)
        shifts = await c.shifts.summary(interaction.guild_id, target.id)
        officer = await self.bot.core.units.officer_by_discord(interaction.guild_id, target.id)
        e = E.base(f"🪪 {target.display_name}", look=look)
        e.set_thumbnail(url=target.display_avatar.url)
        e.add_field(name="Roblox", value=_roblox_line(link), inline=False)
        if staff and staff["status"] != "removed":
            e.add_field(name="Staff", value=f"{E.safe(staff['rank'], 40)}{' · ' + E.safe(staff['department'], 40) if staff.get('department') else ''}"
                                            f"{' · on leave' if staff['status'] == 'leave' else ''}", inline=True)
        if officer:
            e.add_field(name="Unit", value=f"`{officer.get('callsign') or '—'}` · {officer.get('dept_code') or '—'}", inline=True)
        e.add_field(name="Shifts", value=("🟢 On shift now\n" if shifts["current"] else "") +
                    f"This week: {shifts['week_text']}\nAll time: {shifts['total_text']}", inline=True)
        e.add_field(name="Joined", value=discord_ts(target.joined_at.isoformat() if target.joined_at else None, "D"), inline=True)
        await reply(interaction, embed=e)

    member = app_commands.Group(name="member", description="Member directory tools (staff)", guild_only=True)

    @member.command(name="info", description="Full member profile: verification, staff, applications, moderation")
    async def member_info(self, interaction: discord.Interaction, member: discord.User):
        if not await require(interaction, "members.view"):
            return
        c = self.bot.community
        mod = await has_perm(interaction, "mod.view")
        notes = await has_perm(interaction, "members.notes")
        try:
            p = await c.members.profile(interaction.guild_id, member.id, include_cases=mod, include_notes=notes)
        except Exception:
            p = {"roblox": await c.verification.get_link(interaction.guild_id, member.id), "staff": None, "applications": [],
                 "shifts": await c.shifts.summary(interaction.guild_id, member.id), "cases": await c.moderation.history(interaction.guild_id, member.id)}
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"👤 {member}", look=look, description=f"<@{member.id}> · `{member.id}`")
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="Roblox", value=_roblox_line(p.get("roblox")), inline=False)
        st = p.get("staff")
        e.add_field(name="Staff", value=f"{st['rank']} ({st['status']})" if st else "—", inline=True)
        e.add_field(name="Shifts", value=f"{p['shifts']['count']} · {p['shifts']['total_text']}", inline=True)
        apps = p.get("applications") or []
        e.add_field(name="Applications", value="\n".join(f"#{a['number']} {E.safe(a['form_name'], 30)} · {a['status']}" for a in apps[:5]) or "—",
                    inline=False)
        if mod:
            cases = p.get("cases") or []
            e.add_field(name=f"Moderation ({len(cases)})", value="\n".join(
                f"#{x['number']} {x['action']} · {E.safe(x['reason'], 60)} · {x['status']}" for x in cases[:6]) or "Clean record", inline=False)
        if notes and p.get("notes"):
            e.add_field(name="Notes", value="\n".join(f"• {E.safe(n['note'], 120)} — <@{n['author_id']}>" for n in p["notes"][:4]), inline=False)
        await reply(interaction, embed=e)

    @member.command(name="note", description="Add a private staff note to a member")
    async def member_note(self, interaction: discord.Interaction, member: discord.User, note: app_commands.Range[str, 1, 1000]):
        if not await require(interaction, "members.notes"):
            return
        await self.bot.community.members.add_note(interaction.guild_id, member.id, interaction.user.id, note)
        await reply(interaction, f"📝 Note added to {member.mention}.")

    # ------------------------------------------------------------------ verification admin
    verification = app_commands.Group(name="verification", description="Roblox verification tools (staff)", guild_only=True)

    @verification.command(name="lookup", description="Look up a member's Roblox link (or who owns a Roblox account)")
    async def v_lookup(self, interaction: discord.Interaction, member: Optional[discord.User] = None, roblox_username: Optional[str] = None):
        if not await require(interaction, "verification.manage"):
            return
        v = self.bot.community.verification
        if member:
            link = await v.get_link(interaction.guild_id, member.id)
            return await reply(interaction, f"{member.mention}: {_roblox_line(link)}")
        if roblox_username:
            rows = (await v.list_links(interaction.guild_id, q=roblox_username.strip(), per_page=5))["items"]
            if not rows:
                return await reply(interaction, f"No member in this server is verified as **{E.safe(roblox_username, 30)}**.")
            return await reply(interaction, "\n".join(f"<@{r['discord_id']}> → {E.safe(r['roblox_username'], 40)}" for r in rows))
        await reply(interaction, "Give a member or a Roblox username.", error=True)

    @verification.command(name="link", description="Link a member to a Roblox account manually (recorded as a staff link)")
    async def v_link(self, interaction: discord.Interaction, member: discord.Member, roblox_username: str, reason: Optional[str] = None):
        if not await require(interaction, "verification.manage"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = await self.bot.community.verification.manual_link(interaction.guild_id, member.id, roblox_username, interaction.user.id,
                                                                 reason or "")
        msg = f"🔗 {member.mention} is now linked to **{res['roblox']['name']}**."
        if res["problems"]:
            msg += "\n⚠️ " + "\n⚠️ ".join(res["problems"])
        await interaction.followup.send(msg, ephemeral=True)

    @verification.command(name="unlink", description="Remove a member's Roblox link")
    async def v_unlink(self, interaction: discord.Interaction, member: discord.User, reason: Optional[str] = None):
        if not await require(interaction, "verification.manage"):
            return
        ok = await self.bot.community.verification.unlink(interaction.guild_id, member.id, interaction.user.id, reason or "")
        await reply(interaction, f"🗑️ Removed {member.mention}'s Roblox link." if ok else f"{member.mention} is not verified.", error=not ok)

    @verification.command(name="sync", description="Re-apply the verified role and nickname to a member")
    async def v_sync(self, interaction: discord.Interaction, member: discord.Member):
        if not await require(interaction, "verification.manage"):
            return
        problems = await self.bot.community.verification.sync_roles(interaction.guild_id, member.id)
        await reply(interaction, "✅ Roles and nickname updated." if not problems else "⚠️ " + "\n⚠️ ".join(problems), error=bool(problems))

    @verification.command(name="roles", description="Set the roles given/removed after verification")
    async def v_roles(self, interaction: discord.Interaction, verified_role: Optional[discord.Role] = None,
                      unverified_role: Optional[discord.Role] = None):
        if not await require(interaction, "admin.config"):
            return
        s = await self.bot.community.verification.update_settings(
            interaction.guild_id, {"verified_role_id": verified_role.id if verified_role else None,
                                   "unverified_role_id": unverified_role.id if unverified_role else None}, interaction.user.id)
        warn = ""
        me = interaction.guild.me
        for r in (verified_role, unverified_role):
            if r and r >= me.top_role:
                warn += f"\n⚠️ {r.mention} is above my highest role — move my role higher or I can't assign it."
        await reply(interaction, f"✅ Verified role: {_role(s['verified_role_id'])} · Unverified role: {_role(s['unverified_role_id'])}{warn}")

    @verification.command(name="panel", description="Post a public verification panel with a Verify button")
    async def v_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await require(interaction, "verification.manage"):
            return
        ch = channel or interaction.channel
        look = await E.look_for(self.bot, interaction.guild_id)
        view = discord.ui.View(timeout=None)
        view.add_item(VerifyStartItem())
        try:
            await ch.send(embed=verification_panel_embed(look, interaction.guild.name), view=view)
        except discord.HTTPException as e:
            return await reply(interaction, f"Could not post there: {e.text or e}", error=True)
        await self.bot.community.verification.update_settings(interaction.guild_id, {"panel_channel_id": ch.id}, interaction.user.id)
        await reply(interaction, f"✅ Verification panel posted in {ch.mention}.")

    @verification.command(name="history", description="Recent verification activity (optionally for one member)")
    async def v_history(self, interaction: discord.Interaction, member: Optional[discord.User] = None):
        if not await require(interaction, "verification.manage"):
            return
        h = await self.bot.community.verification.history(interaction.guild_id, member.id if member else None, per_page=15)
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = [f"{discord_ts(r['at'], 'f')} **{r['action'].replace('_', ' ')}** <@{r['discord_id']}> → {E.safe(r['roblox_username'] or '—', 30)}"
                 + (f" · by <@{r['actor_id']}>" if r.get("actor_id") and str(r["actor_id"]) != r["discord_id"] else "") for r in h["items"]]
        await reply(interaction, embed=E.base("✅ Verification History", look=look, description="\n".join(lines) or "Nothing yet."))

    @verification.command(name="status", description="Verification settings and numbers for this server")
    async def v_status(self, interaction: discord.Interaction):
        if not await require(interaction, "verification.manage"):
            return
        v = self.bot.community.verification
        s = await v.settings(interaction.guild_id)
        from database.models import iso_now
        stats = await v.stats(interaction.guild_id, iso_now()[:10])
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("✅ Roblox Verification", look=look)
        e.add_field(name="Enabled", value="Yes" if s["enabled"] else "No", inline=True)
        e.add_field(name="Linked members", value=str(stats["linked"]), inline=True)
        e.add_field(name="Verified today", value=str(stats["verified"]), inline=True)
        e.add_field(name="Methods", value=("🔑 Profile code" if s["allow_profile_code"] else "") +
                    ("\n🔐 Roblox sign-in" if s["allow_oauth"] and v.oauth_available() and self.bot.settings.dashboard_public_url else
                     "\n🔐 Roblox sign-in: not set up" if s["allow_oauth"] else ""), inline=True)
        e.add_field(name="Roles", value=f"Verified: {_role(s['verified_role_id'])}\nUnverified: {_role(s['unverified_role_id'])}", inline=True)
        e.add_field(name="Nickname format", value=f"`{s['nickname_format']}`" if s["nickname_format"] else "Unchanged", inline=True)
        e.add_field(name="Minimum account age", value=f"{s['min_account_age_days']} day(s)" if s["min_account_age_days"] else "None", inline=True)
        e.add_field(name="Change settings", value=f"`/verification roles` or the dashboard: {self.bot.settings.dashboard_url}/#/verification",
                    inline=False)
        await reply(interaction, embed=e)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(CommunityMembersCog(bot))

