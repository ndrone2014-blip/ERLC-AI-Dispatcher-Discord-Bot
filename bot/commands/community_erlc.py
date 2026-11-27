"""v5.0 in-game (ER:LC) moderation from Discord: /ingame warn | kick | ban | tempban | unban | note | lookup | players.

Same service as the dashboard's Moderation center, so records made here show up there immediately.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import reply, require
from community.common import fmt_duration
from community.erlc_moderation import ACTION_COLOUR, ACTION_ICON, ACTIONS, PERMISSION
from database.models import discord_ts

from .community_ui import ConfirmView

if TYPE_CHECKING:
    from bot.client import DispatchBot


def record_embed(r: dict, look: E.Look) -> discord.Embed:
    e = E.base(f"{ACTION_ICON[r['action']]} In-game record #{r['number']} · {ACTIONS[r['action']]}", look=look,
               colour=E.MUTED if r["status"] != "active" else ACTION_COLOUR[r["action"]])
    e.add_field(name="Player", value=E.safe(r["roblox_username"], 40) + (f" (`{r['roblox_id']}`)" if r.get("roblox_id") else ""), inline=True)
    e.add_field(name="Moderator", value=f"<@{r['moderator_id']}>", inline=True)
    e.add_field(name="Status", value=r["status"].title(), inline=True)
    if r.get("duration_s"):
        e.add_field(name="Duration", value=fmt_duration(r["duration_s"]), inline=True)
    e.add_field(name="Created", value=discord_ts(r["created_at"], "f"), inline=True)
    e.add_field(name="In-game", value={"sent": "✅ Carried out", "failed": "⚠️ Command failed", "not_requested": "Record only"}[r["in_game"]],
                inline=True)
    e.add_field(name="Reason", value=E.safe(r.get("reason") or "—", 1000), inline=False)
    if r.get("evidence"):
        e.add_field(name="Evidence", value=E.safe(r["evidence"], 500), inline=False)
    if r.get("in_game_detail") and r["in_game"] == "failed":
        e.add_field(name="ER:LC said", value=E.safe(r["in_game_detail"], 300), inline=False)
    return e


class InGameModerationCog(commands.Cog):
    ingame = app_commands.Group(name="ingame", description="In-game (ER:LC) moderation records", guild_only=True)

    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    async def _do(self, interaction: discord.Interaction, action: str, player: str, reason: str, *, evidence: str = "",
                  duration: Optional[str] = None, in_game: bool = False) -> None:
        rec = await self.bot.community.erlc_moderation.create(interaction.guild_id, action, player, interaction.user.id, reason=reason,
                                                               evidence=evidence, duration=duration, execute=in_game, source="discord")
        await reply(interaction, embed=record_embed(rec, await E.look_for(self.bot, interaction.guild_id)))

    async def _confirm(self, interaction: discord.Interaction, action: str, player: str, reason: str, **kw) -> None:
        if not await require(interaction, PERMISSION[action]):
            return
        view = ConfirmView(interaction.user.id, lambda i: self._do(i, action, player, reason, **kw), label=ACTIONS[action])
        where = " and carry it out in-game" if kw.get("in_game") else " (record only)"
        await reply(interaction, f"{ACTION_ICON[action]} {ACTIONS[action]} **{E.safe(player, 40)}**{where}?\n**Reason:** {E.safe(reason, 300)}",
                    view=view)

    @ingame.command(name="warn", description="Record a warning for a Roblox player (optionally PM them in-game)")
    @app_commands.describe(player="Roblox username or user ID", in_game="Also send the warning as an in-game :pm")
    async def warn(self, interaction: discord.Interaction, player: str, reason: app_commands.Range[str, 1, 500],
                   in_game: bool = False, evidence: Optional[str] = None):
        if await require(interaction, PERMISSION["warning"]):
            await self._do(interaction, "warning", player, reason, evidence=evidence or "", in_game=in_game)

    @ingame.command(name="kick", description="Kick a Roblox player (asks for confirmation)")
    @app_commands.describe(player="Roblox username or user ID", in_game="Run :kick in your private server")
    async def kick(self, interaction: discord.Interaction, player: str, reason: app_commands.Range[str, 1, 500],
                   in_game: bool = True, evidence: Optional[str] = None):
        await self._confirm(interaction, "kick", player, reason, evidence=evidence or "", in_game=in_game)

    @ingame.command(name="ban", description="Ban a Roblox player (asks for confirmation)")
    @app_commands.describe(player="Roblox username or user ID", in_game="Run :ban in your private server")
    async def ban(self, interaction: discord.Interaction, player: str, reason: app_commands.Range[str, 1, 500],
                  in_game: bool = True, evidence: Optional[str] = None):
        await self._confirm(interaction, "ban", player, reason, evidence=evidence or "", in_game=in_game)

    @ingame.command(name="tempban", description="Temporarily ban a Roblox player, e.g. 12h, 3d, 2w (lifted automatically)")
    @app_commands.describe(player="Roblox username or user ID", duration="e.g. 12h, 3d, 2w", in_game="Run :ban now and :unban when it ends")
    async def tempban(self, interaction: discord.Interaction, player: str, duration: str, reason: app_commands.Range[str, 1, 500],
                      in_game: bool = True, evidence: Optional[str] = None):
        await self._confirm(interaction, "tempban", player, reason, evidence=evidence or "", duration=duration, in_game=in_game)

    @ingame.command(name="unban", description="Unban a Roblox player")
    @app_commands.describe(player="Roblox username or user ID", in_game="Run :unban in your private server")
    async def unban(self, interaction: discord.Interaction, player: str, reason: app_commands.Range[str, 1, 500], in_game: bool = True):
        if await require(interaction, PERMISSION["unban"]):
            await self._do(interaction, "unban", player, reason, in_game=in_game)

    @ingame.command(name="note", description="Add a moderation note to a Roblox player")
    async def note(self, interaction: discord.Interaction, player: str, note: app_commands.Range[str, 1, 1000]):
        if await require(interaction, "erlc.moderate"):
            await self.bot.community.erlc_moderation.add_note(interaction.guild_id, player, interaction.user.id, note)
            await reply(interaction, f"📝 Note saved for **{E.safe(player, 40)}**.", ephemeral=True)

    @ingame.command(name="lookup", description="A Roblox player's in-game moderation profile")
    async def lookup(self, interaction: discord.Interaction, player: str):
        if not await require(interaction, "erlc.view"):
            return
        p = await self.bot.community.erlc_moderation.profile(interaction.guild_id, player.strip())
        look = await E.look_for(self.bot, interaction.guild_id)
        pl = p["player"]
        e = E.base(f"🎮 {E.safe(pl['username'], 40)}", look=look, colour=E.DANGER if p["active_ban"] else E.BRAND)
        e.add_field(name="Roblox ID", value=pl["roblox_id"] or "Unknown", inline=True)
        e.add_field(name="In-game now", value="Yes" if pl["online"] else "No", inline=True)
        e.add_field(name="Discord", value=f"<@{p['discord_id']}>" if p["discord_id"] else "Not verified", inline=True)
        counts = ", ".join(f"{ACTIONS[k]}: {v}" for k, v in p["counts"].items()) or "No records"
        e.add_field(name="Records", value=counts, inline=False)
        if p["bolos"]:
            active = [b for b in p["bolos"] if b["status"] == "active"]
            if active:
                e.add_field(name="⚠️ Active BOLO", value="\n".join(f"#{b['number']} {E.safe(b['subject'], 80)} ({b['priority']})" for b in active[:3]),
                            inline=False)
        for r in p["records"][:5]:
            e.add_field(name=f"#{r['number']} {ACTIONS[r['action']]} · {r['status']}", value=f"{E.safe(r['reason'], 150)} — {discord_ts(r['created_at'], 'R')}",
                        inline=False)
        await reply(interaction, embed=e, ephemeral=True)

    @ingame.command(name="players", description="Who is in your ER:LC server right now")
    async def players(self, interaction: discord.Interaction):
        if not await require(interaction, "erlc.view"):
            return
        live = self.bot.erlc_live.snapshot(interaction.guild_id) if self.bot.erlc_live else None
        if not live or not live["updated_at"]:
            await reply(interaction, "No live data from ER:LC yet. Check the ER:LC connection (`/erlc`).", ephemeral=True)
            return
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"🎮 {E.safe(live['server'] or 'ER:LC server', 60)} · {live['current_players'] or 0}/{live['max_players'] or '?'}", look=look)
        staff = [p for p in live["players"] if p["staff"]]
        e.add_field(name=f"In-game staff ({len(staff)})", value="\n".join(f"{E.safe(p['name'], 30)} · {p['permission']}" for p in staff[:15]) or "None",
                    inline=False)
        for team, n in sorted(live["teams"].items(), key=lambda kv: -kv[1])[:8]:
            e.add_field(name=team, value=str(n), inline=True)
        e.set_footer(text=f"Updated {live['age_s']}s ago · from the official ER:LC API")
        await reply(interaction, embed=e, ephemeral=True)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(InGameModerationCog(bot))
