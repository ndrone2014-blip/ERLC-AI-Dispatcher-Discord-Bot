"""Shared Discord UI helpers: colours, embed builders, permission guards, safe replies."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord

from database.models import (PRIORITY_EMOJI, PRIORITY_LABEL, SOURCE_LABEL, VERIFICATION_LABEL, discord_ts, loads)
from security.permissions import Actor

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

BRAND = 0x1F6FEB
OK = 0x2EA043
WARN = 0xD29922
DANGER = 0xDA3633
MUTED = 0x6E7681
PRIORITY_COLOUR = {"low": 0x8B949E, "routine": 0x1F6FEB, "medium": 0xD29922, "high": 0xF0883E, "critical": 0xDA3633}
FOOTER = "ER:LC AI Dispatch • Roleplay system — not a real emergency service"


def bot_of(interaction: discord.Interaction) -> "DispatchBot":
    return interaction.client  # type: ignore[return-value]


async def has_perm(interaction: discord.Interaction, permission: str) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    bot = bot_of(interaction)
    return await bot.permissions.has(interaction.guild.id, Actor.from_member(interaction.user), permission)


async def require(interaction: discord.Interaction, permission: str) -> bool:
    if await has_perm(interaction, permission):
        return True
    await reply(interaction, f"⛔ You don't have permission for this action (`{permission}`).", error=True)
    return False


async def reply(interaction: discord.Interaction, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None,
                embeds: Optional[list[discord.Embed]] = None, view: Optional[discord.ui.View] = None, error: bool = False,
                ephemeral: bool = True) -> None:
    kwargs = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if embeds is not None:
        kwargs["embeds"] = embeds
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except discord.HTTPException as e:
        log.warning("Failed to reply to interaction: %s", e)


async def rate_limited(interaction: discord.Interaction, bucket: str = "ui") -> bool:
    bot = bot_of(interaction)
    wait = bot.ratelimiter.check(bucket, interaction.guild_id, interaction.user.id)
    if wait is not None:
        await reply(interaction, f"⏳ Slow down — try again in {int(wait) + 1}s.", error=True)
        return True
    return False


def truncate(text: Optional[str], n: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def priority_badge(inc: dict) -> str:
    p = inc.get("priority") or inc.get("ai_priority")
    if not p:
        return "⏳ Assessing"
    badge = f"{PRIORITY_EMOJI.get(p, '')} {PRIORITY_LABEL.get(p, p)}"
    if inc.get("priority_status") == "recommended":
        badge += " *(awaiting supervisor approval)*"
    elif inc.get("priority_status") == "overridden":
        badge += " *(supervisor override)*"
    return badge


def incident_embed(inc: dict, units: list[dict], *, notes: Optional[list[dict]] = None, detailed: bool = True) -> discord.Embed:
    colour = PRIORITY_COLOUR.get(inc.get("priority") or inc.get("ai_priority") or "", BRAND)
    if inc["status"] == "closed":
        colour = MUTED
    title = f"Incident #{inc['number']} — {truncate(inc.get('nature') or 'Nature not provided', 80)}"
    e = discord.Embed(title=title, colour=colour)
    e.add_field(name="Verification", value=VERIFICATION_LABEL.get(inc["verification"], inc["verification"]), inline=True)
    e.add_field(name="Source", value=SOURCE_LABEL.get(inc["source"], inc["source"]), inline=True)
    e.add_field(name="Status", value=inc["status"].title(), inline=True)
    e.add_field(name="Location", value=truncate(inc.get("location") or "⚠️ Location unavailable", 200), inline=False)
    if inc.get("postal") or inc.get("street"):
        extra = " · ".join(x for x in [inc.get("street"), f"Postal {inc['postal']}" if inc.get("postal") else None] if x)
        e.add_field(name="Caller's position (ER:LC)", value=extra, inline=False)
    if detailed and inc.get("description") and inc.get("description") != inc.get("nature"):
        e.add_field(name="Reported details", value=truncate(inc["description"], 900), inline=False)
    e.add_field(name="Priority", value=priority_badge(inc), inline=True)
    rec_units = inc.get("recommended_units")
    deps = loads(inc.get("recommended_departments"), []) or []
    rec = f"{rec_units} unit(s)" if rec_units else "—"
    if deps:
        rec += f" · {', '.join(deps)}"
    if inc.get("supervisor_recommended"):
        rec += " · 🛡️ Supervisor"
    e.add_field(name="Recommended", value=rec, inline=True)
    e.add_field(name=f"Assigned ({len(units)})", value=", ".join(f"`{u['callsign']}`" for u in units) or "None", inline=True)
    if detailed and inc.get("recommendation_reason"):
        src = inc.get("assessment_source") or ""
        label = "AI assessment" if src.startswith("ai") else "Rule-based assessment"
        e.add_field(name=f"{label} ({src})", value=truncate(inc["recommendation_reason"], 500) +
                    "\n*Recommendation only — based on the information currently available.*", inline=False)
        assess = loads(inc.get("assessment_json"), {}) or {}
        notes_av = assess.get("availability_notes") or []
        if notes_av:
            e.add_field(name="Availability", value=truncate("\n".join(notes_av), 300), inline=False)
    if detailed and notes:
        e.add_field(name="Latest notes", value=truncate("\n".join(f"• {n['detail'].get('author') or ''} {n['detail'].get('note')}"
                                                              for n in notes[-4:]), 700), inline=False)
    if inc.get("erlc_call_number") is not None:
        e.add_field(name="ER:LC call", value=f"#{inc['erlc_call_number']} · team {inc.get('erlc_team') or '—'}", inline=True)
    if inc.get("caller_display"):
        e.add_field(name="Caller", value=truncate(inc["caller_display"], 60), inline=True)
    e.add_field(name="Received", value=discord_ts(inc["created_at"], "f"), inline=True)
    if inc["verification"] == "test":
        e.description = "🧪 **TEST DATA — simulated call, not from ER:LC.**"
    elif inc["verification"] == "unverified":
        e.description = "⚠️ **UNVERIFIED** — not detected through the ER:LC API."
    e.set_footer(text=FOOTER)
    return e


def status_dot(ok: Optional[bool]) -> str:
    return "🟢" if ok else ("🔴" if ok is False else "🟡")
