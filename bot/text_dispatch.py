"""Natural-language dispatch in text (slash commands and the optional dispatch channel).

Speed path: local understanding answers in milliseconds; AI is only consulted when the local
layer cannot interpret the message, and never blocks other users (each request is independent).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import discord

from ai.conversation import Intent, complete_pending, parse_with_ai, strip_wake_word, understand
from security.permissions import Actor
from voice.commands import execute_intent

from . import embeds as E

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

OFFICER_INTENTS = {"status_change", "attach", "detach", "backup", "add_note", "self_initiated"}
QUERY_INTENTS = {"query_highest", "query_active", "query_available", "query_bolos", "query_incident", "query_incident_units",
                 "question", "radio_check"}


@dataclass
class DispatchResult:
    reply: str
    intent: Intent
    elapsed_ms: float
    incident: Optional[dict] = None
    merged: bool = False
    broadcast: str = ""


async def process(bot: "DispatchBot", member: discord.Member, text: str, *, channel_name: str = "") -> DispatchResult:
    started = time.perf_counter()
    guild_id = member.guild.id
    core = bot.core
    memory = bot.voice_commands.memory
    _, body = strip_wake_word(text)
    body = body or text
    statuses = await core.units.status_codes(guild_id)
    officer = await core.units.officer_by_discord(guild_id, member.id)

    intent = understand(body, statuses)
    if intent is None or intent.name == "unclear":
        from ai.conversation import custom_report_intent
        intent = custom_report_intent(body, await bot.db.get_config(guild_id, "custom_categories", []) or []) or intent
    pending = memory.pop_pending(guild_id, member.id)
    if pending is not None and (intent is None or intent.name in {"unclear", "report_incident"} and not
                                (intent.args.get("nature") and intent.args.get("location"))):
        intent = complete_pending(pending, body)
        intent.args["asked_twice"] = pending.get("asked_twice", False)
    if intent is None and core.ai is not None:
        ctx = memory.get(guild_id, member.id)
        try:
            intent = await asyncio.wait_for(parse_with_ai(core.ai, guild_id, body, statuses, ctx,
                                                          officer.get("current_incident_id") if officer else None), 6)
        except asyncio.TimeoutError:
            intent = None
        if intent is not None and intent.name == "question":
            intent.args.setdefault("text", body)
    if intent is None:
        from dispatch import phrases
        intent = Intent("unclear", clarification=phrases.ask_again(guild_id), source="none")

    actor = Actor.from_member(member)
    perms = bot.permissions
    if intent.name == "report_incident":
        if not await perms.has(guild_id, actor, "call.submit"):
            reply = "⛔ You don't have permission to submit calls to dispatch."
            return DispatchResult(reply, intent, (time.perf_counter() - started) * 1000)
    elif intent.name in OFFICER_INTENTS | QUERY_INTENTS:
        if not await perms.has(guild_id, actor, "voice.command"):
            reply = ("Dispatch only takes unit traffic from registered personnel. "
                     "To report an emergency, describe what is happening and where.")
            return DispatchResult(reply, intent, (time.perf_counter() - started) * 1000)

    reply = await execute_intent(core, guild_id, officer, intent, member.id, memory, caller=member.display_name)
    memory.add(guild_id, member.id, officer["callsign"] if officer and officer.get("callsign") else "Caller", body)
    memory.add(guild_id, member.id, "Dispatch", reply)
    elapsed = (time.perf_counter() - started) * 1000

    result = DispatchResult(reply, intent, elapsed)
    last = memory.last_report.pop((guild_id, member.id), None)
    if intent.name == "report_incident" and last is not None:
        inc, merged = last
        result.incident, result.merged = inc, merged
        ann = await bot.db.fetchone("SELECT text FROM announcements WHERE guild_id = ? AND dedupe_key = ?",
                                    (guild_id, f"new911:{inc['id']}"))
        result.broadcast = ann["text"] if ann and not merged else ""
    if intent.name == "unclear" or intent.source == "none":
        await bot.bus.publish("dispatch.failed", guild_id, text=body[:500], reply=reply, actor_id=member.id, channel=channel_name)
    log.info("Text dispatch from %s: %r -> %s (%s) in %.0f ms", member, body[:120], intent.name, intent.source, elapsed)
    return result


async def render(bot: "DispatchBot", guild_id: int, result: DispatchResult) -> list[discord.Embed]:
    look = await E.look_for(bot, guild_id)
    if result.incident is not None:
        units = await bot.core.incidents.assigned_units(result.incident["id"])
        card = E.call_received(result.incident, result.broadcast or result.reply, look=look, merged=result.merged, units=units)
        if result.merged:
            card.description = result.reply
        return [card]
    ok = result.intent.name not in {"unclear"}
    return [E.dispatch_reply(result.reply, look=look, ok=ok, latency_ms=result.elapsed_ms)]


async def on_message(bot: "DispatchBot", message: discord.Message) -> None:
    """Natural-language dispatch channel: anyone can describe an emergency; units can talk to dispatch."""
    if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
        return
    cid = await bot.db.get_config(message.guild.id, "dispatch_channel_id")
    if not cid or int(cid) != message.channel.id:
        return
    if not await bot.core.setting(message.guild.id, "text_dispatch_enabled"):
        return
    text = (message.content or "").strip()
    if not text or text.startswith(("!", "//", "#")) or len(text) > 1500:
        return
    wait = bot.ratelimiter.check("text-dispatch", message.guild.id, message.author.id)
    if wait is not None:
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass
        return
    try:
        async with message.channel.typing():
            result = await process(bot, message.author, text, channel_name=f"#{message.channel.name}")
            embeds = await render(bot, message.guild.id, result)
        await message.reply(embeds=embeds, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.warning("Could not reply in dispatch channel: %s", e)
    except Exception:
        log.exception("Text dispatch failed")
        try:
            await message.reply(embed=E.dispatch_reply("⚠️ Dispatch hit an error processing that. It was logged.", ok=False),
                                mention_author=False)
        except discord.HTTPException:
            pass
