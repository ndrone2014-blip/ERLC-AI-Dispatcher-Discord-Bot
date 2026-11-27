"""v4.0: /ask (Pulse Assistant), /server-info, /bot-info, /announcement."""
from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import reply, require
from core.version import CREDIT, NAME, RELEASE, REPO_URL, VERSION
from database.models import parse_iso, utcnow

from .community_ui import ConfirmView, perms_of

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def _template_ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    rows = await interaction.client.community.embeds.list(interaction.guild_id)
    return [app_commands.Choice(name=t["name"][:100], value=t["id"]) for t in rows if current.lower() in t["name"].lower()][:25]


def reply_embed(r, look: E.Look) -> discord.Embed:
    e = E.base(f"🧠 {r.title}", look=look, colour=(look.accent if r.ok else E.WARN), description=(r.text or None))
    if r.lines:
        body = "\n".join(f"• {E.safe(x, 180)}" for x in r.lines)
        e.description = ((e.description or "") + "\n\n" + body)[:4000]
    src = {"local": "instant answer from live data", "ai": "AI answer · may be imperfect", "none": "no match"}[r.source]
    e.set_footer(text=f"{look.footer()} • {src} • {r.latency_ms:.0f} ms")
    return e


class CommunityAssistantCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    @app_commands.command(name="ask", description="Ask Pulse: active calls, units, shifts, sessions, verification and more")
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 2, 500]):
        if not await require(interaction, "assistant.use"):
            return
        perms = await perms_of(interaction)
        a = self.bot.community.assistant
        from community.assistant import recognise
        if recognise(question) is None:  # only conversational questions reach the AI provider
            await interaction.response.defer(ephemeral=True, thinking=True)
        r = await a.ask(interaction.guild_id, interaction.user.id, perms, question)
        look = await E.look_for(self.bot, interaction.guild_id)
        view = None
        if r.confirm:
            async def go(i: discord.Interaction, action=r.confirm["action"], args=r.confirm["args"]):
                done = await a.confirm(i.guild_id, i.user.id, await perms_of(i), action, args)
                await reply(i, embed=reply_embed(done, look))
            view = ConfirmView(interaction.user.id, go, label=r.confirm["prompt"], danger=r.confirm["action"] == "close_incident")
        await reply(interaction, embed=reply_embed(r, look), view=view)

    @app_commands.command(name="server-info", description="This Discord server and its ER:LC server at a glance")
    @app_commands.guild_only()
    async def server_info(self, interaction: discord.Interaction):
        g = interaction.guild
        c = self.bot.community
        look = await E.look_for(self.bot, g.id)
        counts = await c.members.counts(g.id)
        e = E.base(f"🏙️ {g.name}", look=look)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="Members", value=f"{g.member_count:,}", inline=True)
        e.add_field(name="Verified", value=f"{counts['verified']:,}", inline=True)
        e.add_field(name="Staff", value=str(await c.staff.count(g.id)), inline=True)
        e.add_field(name="On shift", value=str(len(await c.shifts.active(g.id))), inline=True)
        e.add_field(name="Active incidents", value=str(len(await self.bot.core.incidents.list_active(g.id))), inline=True)
        sessions = await c.sessions.list(g.id, "upcoming", 3)
        e.add_field(name="Sessions", value="\n".join(f"#{s['number']} {E.safe(s['name'], 40)} ({s['status']})" for s in sessions) or "None scheduled",
                    inline=True)
        row = await self.bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (g.id,))
        if row and row["connected"]:
            e.add_field(name="ER:LC", value=f"🟢 **{E.safe(row['server_name'] or 'Connected', 60)}** · {row['current_players']}/{row['max_players']} players",
                        inline=False)
        else:
            e.add_field(name="ER:LC", value="⚪ Not connected (an administrator can connect it with `/settings erlc-key`)", inline=False)
        e.add_field(name="Created", value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
        await reply(interaction, embed=e)

    @app_commands.command(name="bot-info", description="Pulse Dispatch status: version, uptime, latency, database, AI, dashboard")
    async def bot_info(self, interaction: discord.Interaction):
        t0 = time.perf_counter()
        db_ok = True
        try:
            await self.bot.db.fetchval("SELECT 1")
        except Exception:
            db_ok = False
        db_ms = (time.perf_counter() - t0) * 1000
        look = await E.look_for(self.bot, interaction.guild_id)
        up = utcnow() - (parse_iso(self.bot.started_at_iso) or utcnow())
        e = E.base(f"📡 {NAME}", look=look, url=REPO_URL, description=f"**v{VERSION}** · {RELEASE}")
        e.add_field(name="Uptime", value=str(dt.timedelta(seconds=int(up.total_seconds()))), inline=True)
        e.add_field(name="Gateway", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        e.add_field(name="Database", value=f"{'🟢' if db_ok else '🔴'} {db_ms:.1f} ms", inline=True)
        e.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        if interaction.guild_id:
            active = self.bot.ai.active_provider(interaction.guild_id)
            e.add_field(name="AI", value=f"{active['label']} · {active['latency_ms']} ms" if active else "Local recognition (no AI call yet)", inline=True)
        web = self.bot.web
        e.add_field(name="Dashboard", value=f"🟢 {self.bot.settings.dashboard_url}" if web and web.running else "⚪ Off", inline=False)
        e.add_field(name="Credits", value=CREDIT, inline=False)
        await reply(interaction, embed=e)

    @app_commands.command(name="announcement", description="Send an announcement (text and/or a saved embed template)")
    @app_commands.autocomplete(template=_template_ac)
    @app_commands.guild_only()
    async def announcement(self, interaction: discord.Interaction, channel: discord.TextChannel, message: Optional[str] = None,
                           template: Optional[int] = None, ping_role: Optional[discord.Role] = None):
        if not await require(interaction, "announcement.send"):
            return
        c = self.bot.community
        if ping_role:
            if ping_role.is_default():
                return await reply(interaction, "Pinging @everyone is not allowed from the bot.", error=True)
            content = f"{ping_role.mention} {message or ''}".strip()
            tpl = await c.embeds.get(interaction.guild_id, template) if template else None
            if template and not tpl:
                return await reply(interaction, "That template no longer exists.", error=True)
            data = tpl["data"] if tpl else None
            from community.embeds import render, validate_embed
            embed = render(validate_embed(data), await c.embeds.values(interaction.guild_id)) if data else None
            _, err = await c.actions.send(interaction.guild_id, channel.id, content=content, embed=embed, mention_roles=[ping_role.id])
            if err:
                return await reply(interaction, f"⚠️ {err}", error=True)
            await self.bot.audit.record(interaction.guild_id, interaction.user.id, "announcement.send", f"#{channel.name}")
        else:
            await c.embeds.send(interaction.guild_id, channel.id, actor_id=interaction.user.id, template_id=template, content=message)
        await reply(interaction, f"📣 Sent to {channel.mention}.")


async def setup(bot: "DispatchBot"):
    await bot.add_cog(CommunityAssistantCog(bot))
