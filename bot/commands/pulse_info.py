"""Information commands (Thanksgiving Update): /about /statistics /ping /version (and the /help content)."""
from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import has_perm, reply, require
from core.version import CREDIT, HIGHLIGHTS, NAME, RELEASE, RELEASE_DATE, REPO_URL, VERSION
from database.models import PRIORITIES, PRIORITY_LABEL, loads, parse_iso, utcnow

if TYPE_CHECKING:
    from bot.client import DispatchBot

HELP_SECTIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "dispatch": ("🚨 Dispatch", [
        ("/dispatch ask", "Talk to dispatch in plain language — report an emergency, set status, ask questions"),
        ("/911", "Submit a roleplay 911 call with a form"),
        ("/incident-view · /incident-update · /incident-close · /incident-close-all", "View, update or close an incident (or close them all)"),
        ("/incident view|create|note|priority|close|history", "Full incident toolkit"),
        ("/calls", "All active incidents"),
        ("/priority", "Approve or change a priority (supervisors)"),
        ("/bolo create|list|clear", "Be On the Look Out alerts"),
        ("/broadcast", "Radio broadcast to all units"),
    ]),
    "units": ("🚓 Units", [
        ("/unit-register", "Register name, callsign and department"),
        ("/unit-status", "Available, En Route, On Scene, Busy, Returning, Out of Service…"),
        ("/unit-assign · /unit-unassign", "Assign / clear units (dispatchers)"),
        ("/unit-list · /available-units", "Unit board and availability"),
        ("/backup", "Request backup (routine, urgent, emergency)"),
        ("/attach · /detach", "Attach yourself to a call"),
    ]),
    "admin": ("🛠️ Administration", [
        ("/setup-guide", "Step-by-step setup: dashboards, ER:LC API key, AI, roles, logs"),
        ("/setup run", "Dispatch Only, Dispatch + Logs, Full or Custom"),
        ("/settings view|…", "Response style, dispatch channel, categories, embeds, ER:LC key…"),
        ("/dashboard status|enable|disable|preset", "Optional dashboards"),
        ("/logs settings|channel|create|toggle|route|view", "Professional logging"),
        ("/departments list|add|remove|status-add|status-remove", "Departments and unit statuses"),
        ("/permissions view|map|set", "Role → permission groups"),
        ("/reset-settings", "Reset settings (data is kept)"),
    ]),
    "ai": ("🧠 AI", [
        ("/ai-status", "Connection status and the active provider"),
        ("/ai-provider list|guide|key|prefer|mode|enable", "15 providers, free-first, per-server keys"),
        ("/ai-test", "Test providers"),
        ("/ai-model", "Pick a model per provider"),
        ("/ai-failover", "Automatic failover on/off and the try order"),
    ]),
    "community": ("👥 Community", [
        ("/verify · /verification lookup|link|unlink|sync|roles|panel|history|status", "Roblox account verification"),
        ("/profile · /member info|note", "Member profiles and staff notes"),
        ("/apply · /application list|review|note|respond|mine|preset|toggle", "Applications and reviews"),
        ("/session create|start|end|list|join", "Community sessions with reminders"),
        ("/announcement", "Send text or a saved embed template"),
        ("/ask", "Ask Pulse: active calls, units, shifts, sessions, verification…"),
    ]),
    "staff": ("🧑‍✈️ Staff & Moderation", [
        ("/staff add|promote|demote|remove|info|list|note|leave", "Staff team management"),
        ("/shift start|end|note|status|active|leaderboard", "Shifts and on-duty tracking"),
        ("/warn · /timeout · /kick · /ban · /unban", "Moderation actions (numbered cases)"),
        ("/history · /case view|revoke|edit", "Moderation history and cases"),
        ("/report-member", "Privately report a member to moderators"),
    ]),
    "info": ("ℹ️ Information", [
        ("/help", "This menu"), ("/about", "About Pulse Dispatch"), ("/statistics", "Dispatch statistics"),
        ("/ping", "Latency check"), ("/version", "Version and release notes"),
        ("/server-info · /bot-info", "Server overview and bot status"),
        ("/dashboard web", "Link to the web dashboard"),
        ("/voice join|leave|listen|mute|test|diagnose", "Dispatcher voice"),
    ]),
}


def help_embed(section: str, look: E.Look) -> discord.Embed:
    if section == "home":
        e = E.base(f"📡 {NAME} — Help", look=look, description=(
            "Talk to dispatch naturally: *“we have a vehicle crash at the bank”*, *“need EMS at the gas station”*, "
            "*“show me 10-8”*. Use the dispatch channel, `/dispatch ask`, or the 📻 radio.\n\n"
            "In-game 911 calls arrive automatically through the official ER:LC API.\n"
            "New here? Run **`/setup-guide`**. Pick a category below for every command."))
        for key, (title, items) in HELP_SECTIONS.items():
            e.add_field(name=title, value=f"{len(items)} commands", inline=True)
        e.add_field(name="Roleplay only", value="Pulse Dispatch is part of a roleplay community — not a real emergency service.",
                    inline=False)
        return e
    title, items = HELP_SECTIONS[section]
    e = E.base(f"{title} Commands", look=look)
    for cmd, desc in items:
        e.add_field(name=cmd, value=desc, inline=False)
    return e


class HelpView(discord.ui.View):
    def __init__(self, look: E.Look):
        super().__init__(timeout=300)
        self.look = look
        sel = discord.ui.Select(placeholder="Choose a category…", options=[discord.SelectOption(label="Overview", value="home", emoji="📡")]
                                + [discord.SelectOption(label=t.split(" ", 1)[1], value=k, emoji=t.split(" ", 1)[0])
                                   for k, (t, _) in HELP_SECTIONS.items()])
        sel.callback = self._pick
        self.sel = sel
        self.add_item(sel)

    async def _pick(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=help_embed(self.sel.values[0], self.look), view=self)


async def statistics_data(bot: "DispatchBot", guild_id: int) -> dict:
    from panels.statistics import gather
    s = await gather(bot, guild_id)
    db = bot.db
    s["calls_911"] = int(await db.fetchval("SELECT COUNT(*) FROM incidents WHERE guild_id = ? AND source IN ('erlc', 'discord')",
                                           (guild_id,), 0))
    rows = await db.fetchall("SELECT recommended_departments FROM incidents WHERE guild_id = ? AND recommended_departments IS NOT NULL "
                             "ORDER BY id DESC LIMIT 2000", (guild_id,))
    by_dept: dict[str, int] = {}
    for r in rows:
        for d in loads(r["recommended_departments"], []) or []:
            by_dept[d] = by_dept.get(d, 0) + 1
    s["by_department"] = sorted(by_dept.items(), key=lambda x: -x[1])[:8]
    rows = await db.fetchall(
        "SELECT i.created_at, MIN(a.assigned_at) AS first FROM incidents i JOIN incident_assignments a ON a.incident_id = i.id "
        "WHERE i.guild_id = ? GROUP BY i.id ORDER BY i.id DESC LIMIT 500", (guild_id,))
    deltas = []
    for r in rows:
        a, b = parse_iso(r["created_at"]), parse_iso(r["first"])
        if a and b and b >= a:
            deltas.append((b - a).total_seconds())
    s["avg_response_all_s"] = sum(deltas) / len(deltas) if deltas else None
    return s


def _fmt_secs(v) -> str:
    if v is None:
        return "—"
    return f"{v:.0f} s" if v < 90 else f"{v / 60:.1f} min"


class PulseInfoCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    @app_commands.command(name="about", description="About Pulse Dispatch | ERLC")
    async def about(self, interaction: discord.Interaction):
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"📡 {NAME}", look=look, url=REPO_URL, description=(
            "An intelligent, realistic dispatch operation inside Discord for Emergency Response: Liberty County roleplay "
            "communities: automatic in-game 911 calls, natural-language dispatch, radio voice, unit tracking, "
            "professional logging and free AI with automatic failover.\n\n*Roleplay only — not a real emergency service.*"))
        e.add_field(name="Version", value=f"{VERSION} · {RELEASE}", inline=True)
        e.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        e.add_field(name="Credits", value=CREDIT, inline=False)
        await reply(interaction, embed=e)

    @app_commands.command(name="version", description="Version and what's new in this release")
    async def version(self, interaction: discord.Interaction):
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"🦃 {NAME} — {RELEASE}", look=look, colour=0xD9822B, url=REPO_URL,
                   description=f"**Version {VERSION}** · released {RELEASE_DATE}\n\n" + "\n".join(f"• {h}" for h in HIGHLIGHTS))
        e.add_field(name="Credits", value=CREDIT, inline=False)
        await reply(interaction, embed=e)

    @app_commands.command(name="ping", description="Check Discord, database and AI latency")
    async def ping(self, interaction: discord.Interaction):
        t0 = time.perf_counter()
        await self.bot.db.fetchval("SELECT 1")
        db_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        from dispatch import nlu
        nlu.analyze("we have a vehicle crash at the bank, need ems")
        nlu_ms = (time.perf_counter() - t1) * 1000
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🏓 Pong", look=look, colour=E.OK)
        e.add_field(name="Discord gateway", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        e.add_field(name="Database", value=f"{db_ms:.1f} ms", inline=True)
        e.add_field(name="Local recognition", value=f"{nlu_ms:.2f} ms", inline=True)
        if interaction.guild_id:
            active = self.bot.ai.active_provider(interaction.guild_id)
            e.add_field(name="Last AI reply", value=f"{active['label']} · {active['latency_ms']} ms" if active else "—", inline=True)
            sync = self.bot.sync.guilds.get(interaction.guild_id)
            e.add_field(name="ER:LC sync", value="running" if sync and sync.task and not sync.task.done() else "not running", inline=True)
        up = utcnow() - (parse_iso(self.bot.started_at_iso) or utcnow())
        e.add_field(name="Uptime", value=str(dt.timedelta(seconds=int(up.total_seconds()))), inline=True)
        await reply(interaction, embed=e)

    @app_commands.command(name="statistics", description="Dispatch statistics (calls, incidents, response times, units)")
    @app_commands.guild_only()
    async def statistics(self, interaction: discord.Interaction):
        if not await require(interaction, "stats.view"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = await statistics_data(self.bot, interaction.guild_id)
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("📊 Dispatch Statistics", look=look)
        e.add_field(name="911 calls", value=str(s["calls_911"]), inline=True)
        e.add_field(name="Incidents", value=str(s["total"]), inline=True)
        e.add_field(name="Last 24 h", value=str(s["last24"]), inline=True)
        e.add_field(name="Open", value=str(s["active"]), inline=True)
        e.add_field(name="Closed", value=str(s["closed"]), inline=True)
        av = s["availability"]
        e.add_field(name="Units available / assigned", value=f"{av['available']} / {av['assigned']}", inline=True)
        e.add_field(name="Avg time to first unit", value=f"24 h: {_fmt_secs(s['avg_response_s'])}\nAll time: {_fmt_secs(s['avg_response_all_s'])}",
                    inline=True)
        bp = s["by_priority"]
        e.add_field(name="By priority", value="\n".join(f"{E.PRIORITY_DOT[p]} {PRIORITY_LABEL[p]}: {bp.get(p, 0)}"
                                                        for p in reversed(PRIORITIES)), inline=True)
        e.add_field(name="By department", value="\n".join(f"`{d}` {n}" for d, n in s["by_department"]) or "—", inline=True)
        if await has_perm(interaction, "report.view_all"):
            e.add_field(name="Top categories", value="\n".join(f"{E.safe(c, 40)}: {n}" for c, n in s["by_category"][:6]) or "—",
                        inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(PulseInfoCog(bot))
