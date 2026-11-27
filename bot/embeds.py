"""Pulse Dispatch embed design system.

One consistent look for every dispatch message and log entry: professional titles,
a single icon per title, colour-coded priorities, timestamps, organised fields and the
"Pulse Dispatch | ERLC" footer. Per-server appearance comes from `embed_settings`
(accent colour, compact mode, footer note) - see /settings embed.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import discord

from database.models import PRIORITY_LABEL, SOURCE_LABEL, VERIFICATION_LABEL, loads, parse_iso

FOOTER_TEXT = "Pulse Dispatch | ERLC"
BRAND = 0x1F6FEB
OK = 0x2EA043
WARN = 0xD29922
DANGER = 0xDA3633
MUTED = 0x6E7681
INFO = 0x8957E5
PRIORITY_COLOUR = {"low": 0x8B949E, "routine": 0x1F6FEB, "medium": 0xD29922, "high": 0xF0883E, "critical": 0xDA3633}
PRIORITY_DOT = {"low": "⚪", "routine": "🔵", "medium": "🟡", "high": "🟠", "critical": "🔴"}

from core.branding import DEFAULT_EMBED_SETTINGS  # noqa: E402  (re-exported)


class Look:
    """Per-guild appearance (resolved once per message)."""

    def __init__(self, settings: Optional[dict] = None):
        s = {**DEFAULT_EMBED_SETTINGS, **(settings or {})}
        self.accent = int(s.get("accent") or BRAND)
        self.compact = bool(s.get("compact"))
        self.footer_note = str(s.get("footer_note") or "")[:60]

    def footer(self) -> str:
        return f"{FOOTER_TEXT} • {self.footer_note}" if self.footer_note else FOOTER_TEXT


async def look_for(bot, guild_id: Optional[int]) -> Look:
    if guild_id is None:
        return Look()
    return Look(await bot.db.get_config(guild_id, "embed_settings", {}) or {})


def base(title: str, *, description: Optional[str] = None, colour: Optional[int] = None, look: Optional[Look] = None,
         timestamp: bool = True, url: Optional[str] = None) -> discord.Embed:
    look = look or Look()
    e = discord.Embed(title=title[:256], description=(description or None) and description[:4000],
                      colour=colour if colour is not None else look.accent, url=url)
    if timestamp:
        e.timestamp = dt.datetime.now(dt.timezone.utc)
    e.set_footer(text=look.footer())
    return e


def priority_of(inc: dict) -> str:
    return inc.get("priority") or inc.get("ai_priority") or ""


def priority_text(inc: dict) -> str:
    p = priority_of(inc)
    if not p:
        return "⏳ Assessing"
    t = f"{PRIORITY_DOT.get(p, '')} {PRIORITY_LABEL.get(p, p)}"
    if inc.get("priority_status") == "recommended" and PRIORITY_LABEL.get(p):
        t += " · awaiting approval"
    elif inc.get("priority_status") == "overridden":
        t += " · supervisor override"
    return t


def status_text(inc: dict, units: Optional[list] = None) -> str:
    if inc.get("status") == "closed":
        return "🔒 Closed"
    if units:
        return f"🚓 {len(units)} unit{'s' if len(units) != 1 else ''} assigned"
    return "📻 Awaiting Units"


def _ts(value: Optional[str], style: str = "R") -> str:
    d = parse_iso(value)
    return f"<t:{int(d.timestamp())}:{style}>" if d else "—"


def call_received(inc: dict, dispatch_text: str, *, look: Optional[Look] = None, merged: bool = False,
                  units: Optional[list] = None) -> discord.Embed:
    """'911 CALL RECEIVED' card shown to the reporter and in the log."""
    title = "📞 Call Added to Active Incident" if merged else "🚨 911 Call Received"
    e = base(title, colour=PRIORITY_COLOUR.get(priority_of(inc), (look or Look()).accent), look=look)
    e.add_field(name="Incident", value=f"#{inc['number']} · {inc.get('nature') or inc.get('category') or 'Unknown'}", inline=True)
    e.add_field(name="Location", value=(inc.get("location") or "⚠️ Not provided")[:200], inline=True)
    e.add_field(name="Priority", value=priority_text(inc), inline=True)
    e.add_field(name="Status", value=status_text(inc, units), inline=True)
    e.add_field(name="Verification", value=VERIFICATION_LABEL.get(inc.get("verification"), "—"), inline=True)
    e.add_field(name="Received", value=_ts(inc.get("created_at")), inline=True)
    if dispatch_text:
        e.add_field(name="📡 Dispatch", value=f"“{dispatch_text[:900]}”", inline=False)
    return e


def dispatch_reply(text: str, *, look: Optional[Look] = None, ok: bool = True, title: str = "📡 Dispatch",
                   latency_ms: Optional[float] = None) -> discord.Embed:
    e = base(title, description=text, colour=(look or Look()).accent if ok else WARN, look=look)
    if latency_ms is not None and not (look and look.compact):
        e.set_footer(text=f"{(look or Look()).footer()} • {latency_ms:.0f} ms")
    return e


def incident_summary_line(inc: dict, units: Optional[list] = None) -> str:
    p = priority_of(inc)
    return (f"**#{inc['number']}** {PRIORITY_DOT.get(p, '⏳')} {(inc.get('nature') or 'Unknown')[:50]} · "
            f"{(inc.get('location') or 'no location')[:40]} · {len(units or [])} unit(s) · {_ts(inc.get('created_at'))}")


def recommended_text(inc: dict) -> str:
    deps = loads(inc.get("recommended_departments"), []) or []
    n = inc.get("recommended_units")
    out = f"{n} unit(s)" if n else "—"
    if deps:
        out += " · " + ", ".join(deps)
    return out


def source_text(inc: dict) -> str:
    return SOURCE_LABEL.get(inc.get("source"), inc.get("source") or "—")


def safe(text: Optional[str], n: int = 1000) -> str:
    """Escape markdown + mentions in user-supplied text shown inside embeds."""
    t = discord.utils.escape_mentions(discord.utils.escape_markdown(str(text or "")))
    return t if len(t) <= n else t[: n - 1] + "…"
