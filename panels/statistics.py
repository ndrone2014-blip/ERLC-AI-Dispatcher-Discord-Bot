"""Panel 11: Statistics (all numbers come from stored data)."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import discord

from bot.ui import BRAND, FOOTER, bot_of, reply, require
from database.models import parse_iso, utcnow

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def gather(bot: "DispatchBot", guild_id: int) -> dict:
    db = bot.db
    since = (utcnow() - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    s = {
        "total": await db.fetchval("SELECT COUNT(*) FROM incidents WHERE guild_id = ?", (guild_id,), 0),
        "closed": await db.fetchval("SELECT COUNT(*) FROM incidents WHERE guild_id = ? AND status = 'closed'", (guild_id,), 0),
        "active": await db.fetchval("SELECT COUNT(*) FROM incidents WHERE guild_id = ? AND status != 'closed'", (guild_id,), 0),
        "last24": await db.fetchval("SELECT COUNT(*) FROM incidents WHERE guild_id = ? AND created_at >= ?", (guild_id, since), 0),
        "pending_reports": await db.fetchval("SELECT COUNT(*) FROM reports WHERE guild_id = ? AND status != 'closed'", (guild_id,), 0),
        "active_bolos": await db.fetchval("SELECT COUNT(*) FROM bolos WHERE guild_id = ? AND status = 'active'", (guild_id,), 0),
        "by_source": {r["source"]: r["n"] for r in await db.fetchall(
            "SELECT source, COUNT(*) n FROM incidents WHERE guild_id = ? GROUP BY source", (guild_id,))},
        "by_category": [(r["category"] or "Unclassified", r["n"]) for r in await db.fetchall(
            "SELECT category, COUNT(*) n FROM incidents WHERE guild_id = ? GROUP BY category ORDER BY n DESC LIMIT 8", (guild_id,))],
        "by_priority": {r["priority"] or "unassessed": r["n"] for r in await db.fetchall(
            "SELECT priority, COUNT(*) n FROM incidents WHERE guild_id = ? GROUP BY priority", (guild_id,))},
    }
    rows = await db.fetchall(
        "SELECT i.created_at, MIN(a.assigned_at) AS first FROM incidents i JOIN incident_assignments a ON a.incident_id = i.id "
        "WHERE i.guild_id = ? AND i.created_at >= ? GROUP BY i.id", (guild_id, since))
    deltas = []
    for r in rows:
        a, b = parse_iso(r["created_at"]), parse_iso(r["first"])
        if a and b and b >= a:
            deltas.append((b - a).total_seconds())
    s["avg_response_s"] = sum(deltas) / len(deltas) if deltas else None
    s["availability"] = await bot.core.units.availability_summary(guild_id)
    return s


async def build(bot: "DispatchBot", guild_id: int):
    s = await gather(bot, guild_id)
    e = discord.Embed(title="📊 Dispatch Statistics", colour=BRAND)
    e.add_field(name="Total incidents", value=str(s["total"]), inline=True)
    e.add_field(name="Completed", value=str(s["closed"]), inline=True)
    e.add_field(name="Active", value=str(s["active"]), inline=True)
    e.add_field(name="Last 24 h", value=str(s["last24"]), inline=True)
    e.add_field(name="Pending reports", value=str(s["pending_reports"]), inline=True)
    e.add_field(name="Active BOLOs", value=str(s["active_bolos"]), inline=True)
    av = s["availability"]
    e.add_field(name="Units available / assigned", value=f"{av['available']} / {av['assigned']}", inline=True)
    resp = s["avg_response_s"]
    e.add_field(name="Avg time to first unit (24 h)", value=f"{resp / 60:.1f} min" if resp is not None else "—", inline=True)
    src = s["by_source"]
    e.add_field(name="By source", value=(f"ER:LC {src.get('erlc', 0)} · Discord {src.get('discord', 0)} · "
                                         f"Manual {src.get('manual', 0)} · Test {src.get('test', 0)}"), inline=False)
    e.add_field(name="Calls by category", value="\n".join(f"`{n:>3}` {c}" for c, n in s["by_category"]) or "No data", inline=True)
    e.add_field(name="By priority", value="\n".join(f"`{n:>3}` {p}" for p, n in s["by_priority"].items()) or "No data", inline=True)
    deps = av["by_department"]
    if deps:
        e.add_field(name="Department activity (units)", value="\n".join(
            f"`{c}` {d['available']} avail · {d['assigned']} assigned" for c, d in sorted(deps.items())), inline=False)
    e.set_footer(text=FOOTER)
    return [e], StatsView()


async def shift_summary(bot: "DispatchBot", guild_id: int, hours: int = 24) -> list[tuple[str, float, int]]:
    """Per-unit on-duty minutes and incidents handled in the window, from status history."""
    since_dt = utcnow() - dt.timedelta(hours=hours)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    units = await bot.db.fetchall("SELECT id, callsign FROM units WHERE guild_id = ?", (guild_id,))
    codes = {c["code"]: c["availability"] for c in await bot.core.units.status_codes(guild_id)}
    out = []
    for u in units:
        hist = await bot.db.fetchall(
            "SELECT new_code, at FROM unit_status_history WHERE unit_id = ? AND at >= ? ORDER BY id", (u["id"], since))
        prev = await bot.db.fetchone(
            "SELECT new_code FROM unit_status_history WHERE unit_id = ? AND at < ? ORDER BY id DESC LIMIT 1", (u["id"], since))
        on = prev is not None and codes.get(prev["new_code"]) in {"available", "assigned"}
        t0 = since_dt
        total = 0.0
        for h in hist:
            at = parse_iso(h["at"]) or t0
            if on:
                total += (at - t0).total_seconds()
            on = codes.get(h["new_code"]) in {"available", "assigned"}
            t0 = at
        if on:
            total += (utcnow() - t0).total_seconds()
        handled = await bot.db.fetchval(
            "SELECT COUNT(DISTINCT incident_id) FROM incident_assignments WHERE unit_id = ? AND assigned_at >= ?", (u["id"], since), 0)
        if total > 0 or handled:
            out.append((u["callsign"], total / 60, handled))
    return sorted(out, key=lambda x: -x[1])


class StatsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Shift Summary (24 h)", emoji="🕒", style=discord.ButtonStyle.secondary, custom_id="cad:stats:shift")
    async def shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "stats.view"):
            return
        rows = await shift_summary(bot_of(interaction), interaction.guild_id)
        e = discord.Embed(title="🕒 Shift summary — last 24 hours", colour=BRAND,
                          description="\n".join(f"`{c:<10}` {m:6.0f} min on duty · {n} incident(s)" for c, m, n in rows[:25])
                          or "No duty activity recorded in the last 24 hours.")
        await reply(interaction, embed=e)

    @discord.ui.button(label="Recent Closed Incidents", emoji="📁", style=discord.ButtonStyle.secondary, custom_id="cad:stats:closed")
    async def closed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "incident.view"):
            return
        bot = bot_of(interaction)
        rows = await bot.core.incidents.list_recent_closed(interaction.guild_id, 15)
        from database.models import discord_ts
        e = discord.Embed(title="📁 Recently closed incidents", colour=BRAND, description="\n".join(
            f"#{r['number']} {r.get('nature') or '—'} · {r.get('location') or '—'} · closed {discord_ts(r['closed_at'])}" for r in rows)
            or "None yet.")
        await reply(interaction, embed=e)
