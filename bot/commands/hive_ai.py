"""AI commands (Thanksgiving Update): /ai-status /ai-provider /ai-test /ai-model /ai-failover.

Keys typed by administrators are entered in a private modal, encrypted at rest, scoped to that
server only, and never shown again (only a masked preview).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ai.providers.base import ProviderError
from ai.providers.catalog import CATALOG, COST_LABEL, SPEC, looks_like_key
from bot import embeds as E
from bot.ui import reply, require
from core.logging_setup import redact
from security.secrets import mask

if TYPE_CHECKING:
    from bot.client import DispatchBot

KEYED = [s for s in CATALOG if s.needs_key]
_model_cache: dict[tuple[int, str], tuple[float, list[str]]] = {}


async def provider_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [app_commands.Choice(name=f"{s.label} — {COST_LABEL[s.cost]}"[:100], value=s.name) for s in CATALOG
            if cur in s.name or cur in s.label.lower()][:25]


async def keyed_provider_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [app_commands.Choice(name=f"{s.label} — {COST_LABEL[s.cost]}"[:100], value=s.name) for s in KEYED
            if cur in s.name or cur in s.label.lower()][:25]


async def _models_for(bot: "DispatchBot", guild_id: int, provider: str) -> list[str]:
    key = (guild_id, provider)
    hit = _model_cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    p = bot.ai.providers.get(provider)
    if p is None:
        return []
    creds = await bot._ai_credentials(guild_id, provider)
    try:
        models = await p.list_models(**creds)
    except ProviderError:
        sp = SPEC.get(provider)
        models = list(sp.models) if sp else [p.default_model]
    _model_cache[key] = (time.monotonic(), models)
    return models


async def model_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    provider = getattr(interaction.namespace, "provider", None)
    if not provider:
        return []
    models = await _models_for(interaction.client, interaction.guild_id, provider)
    cur = current.lower()
    return [app_commands.Choice(name=m[:100], value=m[:100]) for m in models if cur in m.lower()][:25]


def _state(p: dict) -> str:
    if not p["configured"]:
        return "⚫ No key"
    if not p["enabled"]:
        return "⚪ Disabled"
    if p["cooling_down"]:
        return f"🟡 Paused {max(0, int(p['cooldown_until'] - time.time()))}s"
    if p.get("last_error_at") and (not p.get("last_ok_at") or p["last_error_at"] > p["last_ok_at"]):
        return "🟠 Ready (last request failed)"
    return "🟢 Ready"


class KeyModal(discord.ui.Modal):
    def __init__(self, provider: str):
        sp = SPEC[provider]
        super().__init__(title=f"{sp.label} API key"[:45])
        self.provider = provider
        self.key = discord.ui.TextInput(label="API key (stored encrypted, never shown)", max_length=400,
                                        placeholder=f"{sp.key_prefix}…" if sp.key_prefix else "Paste your key")
        self.add_item(self.key)
        self.account = None
        if sp.needs_account_id:
            self.account = discord.ui.TextInput(label="Cloudflare Account ID", max_length=64, min_length=10)
            self.add_item(self.account)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        bot: "DispatchBot" = interaction.client  # type: ignore[assignment]
        sp = SPEC[self.provider]
        key = self.key.value.strip()
        warning = looks_like_key(self.provider, key)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.secrets.set(interaction.guild_id, f"ai_key:{self.provider}", key, interaction.user.id)
        if self.account is not None:
            await bot.secrets.set(interaction.guild_id, f"ai_account:{self.provider}", self.account.value.strip(), interaction.user.id)
        await bot.ai.clear_cooldown(interaction.guild_id, self.provider)
        await bot.ai.set_enabled(interaction.guild_id, self.provider, True)
        _model_cache.pop((interaction.guild_id, self.provider), None)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "ai.key_set", self.provider, {"key": "***"})
        ok, msg = await bot.ai.test_provider(interaction.guild_id, self.provider)
        look = await E.look_for(bot, interaction.guild_id)
        e = E.base(f"🔑 {sp.label} key saved", colour=E.OK if ok else E.WARN, look=look,
                   description=f"Stored encrypted for this server only: `{mask(key)}`")
        e.add_field(name="Connection test", value=("🟢 " if ok else "🔴 ") + E.safe(redact(msg), 300), inline=False)
        if warning:
            e.add_field(name="Check", value=warning, inline=False)
        e.add_field(name="Cost", value=f"{sp.cost_label} — {sp.free_tier}"[:1024], inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)


class HiveAICog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    async def _status_embed(self, guild_id: int) -> discord.Embed:
        bot = self.bot
        rows = await bot.ai.ordered_providers(guild_id)
        settings = await bot.ai.ai_settings(guild_id)
        active = bot.ai.active_provider(guild_id)
        usage = await bot.ai.usage_summary(guild_id)
        look = await E.look_for(bot, guild_id)
        e = E.base("🧠 AI Status", look=look, description=(
            f"Mode: **{'Automatic' if settings['mode'] == 'auto' else 'Manual order'}** · "
            f"Failover: **{'on' if settings['failover'] else 'off'}** · Preferred: **{settings.get('preferred') or '—'}**\n"
            + (f"Currently active: **{active['label']}** (`{active['model']}`, {active['latency_ms']} ms, <t:{int(active['at'])}:R>)"
               if active else "Currently active: none yet this session")
            + f"\nResponse cache hits: {bot.ai.cache_hits} · Dispatch keeps working with no AI (local recognition)."))
        ready = [p for p in rows if p["configured"]]
        for i, p in enumerate(ready[:12]):
            u = usage.get(p["provider"], {})
            src = {"server": "server key", "env": ".env key"}.get(p.get("key_source") or "", "")
            e.add_field(name=f"{i + 1}. {p['label']}", value=(
                f"{_state(p)} · {COST_LABEL.get(p['cost'], '')}\n`{p['effective_model'][:60]}`\n"
                f"Today: {p['requests_today']} req · {u.get('failed') or 0} failed · avg {int(u.get('avg_ms') or 0)} ms"
                + (f" · {src}" if src else "")), inline=True)
        missing = [p for p in rows if not p["configured"] and SPEC.get(p["provider"]) and SPEC[p["provider"]].is_free]
        if missing:
            e.add_field(name="Free providers you can add", value=", ".join(p["label"] for p in missing)[:1000]
                        + "\n→ `/ai-provider list` for links, `/ai-provider key` to add one.", inline=False)
        warnings = await bot.ai.quota_warnings(guild_id)
        if warnings:
            e.add_field(name="⚠️ Warnings", value=E.safe(redact("\n".join(warnings)), 1000), inline=False)
        return e

    @app_commands.command(name="ai-status", description="AI provider connection status, active provider and usage")
    @app_commands.guild_only()
    async def ai_status(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=await self._status_embed(interaction.guild_id), ephemeral=True)

    provider = app_commands.Group(name="ai-provider", description="Choose and configure AI providers", guild_only=True)

    @provider.command(name="list", description="Every supported provider, its cost and where to get a key")
    async def provider_list(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        rows = {p["provider"]: p for p in await self.bot.ai.guild_providers(interaction.guild_id)}
        look = await E.look_for(self.bot, interaction.guild_id)
        embeds = []
        e = E.base("🤖 AI Providers", look=look, description=(
            "**Free-first.** 🟢 = permanent free tier · 🟡 = small credits / trial only · 🔴 = paid · 🖥️ = runs on your PC.\n"
            "Add a key with `/ai-provider key`. Free-tier limits change often — check the provider's page."))
        for sp in CATALOG:
            if len(e.fields) >= 8:
                embeds.append(e)
                e = E.base("🤖 AI Providers (continued)", look=look)
            st = rows.get(sp.name)
            have = "✅ configured" if st and st["configured"] else "—"
            link = f"[Get a key]({sp.signup_url})" if sp.signup_url else ""
            e.add_field(name=f"{sp.label} · {sp.cost_label}", value=f"{sp.free_tier}\n{link} · {have}"[:1024], inline=False)
        embeds.append(e)
        await reply(interaction, embeds=embeds[:10])

    @provider.command(name="guide", description="Step-by-step: how to get an API key for a provider")
    @app_commands.autocomplete(name=provider_autocomplete)
    async def provider_guide(self, interaction: discord.Interaction, name: str):
        if not await require(interaction, "admin.ai"):
            return
        sp = SPEC.get(name)
        if sp is None:
            return await reply(interaction, "Unknown provider.", error=True)
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base(f"🔑 {sp.label}", look=look, url=sp.signup_url or None, description=sp.how_to)
        e.add_field(name="Cost", value=f"{sp.cost_label}\n{sp.free_tier}"[:1024], inline=False)
        if sp.signup_url:
            e.add_field(name="Key page", value=sp.signup_url, inline=False)
        if sp.docs_url:
            e.add_field(name="Limits / docs", value=sp.docs_url, inline=False)
        e.add_field(name="Then", value=(f"`/ai-provider key name:{sp.name}`" if sp.needs_key
                                        else f"Set `{sp.key_env}` in the bot's .env file and restart."), inline=False)
        await reply(interaction, embed=e)

    @provider.command(name="key", description="Add or replace this server's API key for a provider (private, encrypted)")
    @app_commands.autocomplete(name=keyed_provider_autocomplete)
    async def provider_key(self, interaction: discord.Interaction, name: str):
        if not await require(interaction, "admin.ai"):
            return
        if name not in SPEC or not SPEC[name].needs_key:
            return await reply(interaction, "Pick a provider from the list (Ollama/custom endpoints are configured in .env).", error=True)
        await interaction.response.send_modal(KeyModal(name))

    @provider.command(name="remove-key", description="Delete this server's stored key for a provider")
    @app_commands.autocomplete(name=keyed_provider_autocomplete)
    async def provider_remove_key(self, interaction: discord.Interaction, name: str):
        if not await require(interaction, "admin.ai"):
            return
        await self.bot.secrets.delete(interaction.guild_id, f"ai_key:{name}")
        await self.bot.secrets.delete(interaction.guild_id, f"ai_account:{name}")
        _model_cache.pop((interaction.guild_id, name), None)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.key_removed", name)
        await reply(interaction, f"🗑️ Server key for **{SPEC[name].label if name in SPEC else name}** deleted.")

    @provider.command(name="prefer", description="Use this provider first (empty = no preference)")
    @app_commands.autocomplete(name=provider_autocomplete)
    async def provider_prefer(self, interaction: discord.Interaction, name: Optional[str] = None):
        if not await require(interaction, "admin.ai"):
            return
        if name and name not in self.bot.ai.providers:
            return await reply(interaction, "Unknown provider.", error=True)
        await self.bot.ai.update_ai_settings(interaction.guild_id, preferred=name or None)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.preferred", name or "none")
        rows = {p["provider"]: p for p in await self.bot.ai.guild_providers(interaction.guild_id)}
        warn = "" if not name or rows.get(name, {}).get("configured") else " ⚠️ It has no key yet — add one with `/ai-provider key`."
        await reply(interaction, f"⭐ Preferred provider: **{SPEC[name].label if name else 'none'}**.{warn}")

    @provider.command(name="mode", description="Automatic (healthiest free provider first) or Manual (your order)")
    @app_commands.choices(mode=[app_commands.Choice(name="Automatic — pick an available provider", value="auto"),
                                app_commands.Choice(name="Manual — use my order (/ai primary)", value="manual")])
    async def provider_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not await require(interaction, "admin.ai"):
            return
        await self.bot.ai.update_ai_settings(interaction.guild_id, mode=mode.value)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.mode", mode.value)
        await reply(interaction, f"🧭 AI mode: **{mode.name}**. Takes effect immediately (no restart).")

    @provider.command(name="enable", description="Enable or disable a provider for this server")
    @app_commands.autocomplete(name=provider_autocomplete)
    async def provider_enable(self, interaction: discord.Interaction, name: str, enabled: bool):
        if not await require(interaction, "admin.ai"):
            return
        if name not in self.bot.ai.providers:
            return await reply(interaction, "Unknown provider.", error=True)
        await self.bot.ai.set_enabled(interaction.guild_id, name, enabled)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.enabled", name, {"enabled": enabled})
        await reply(interaction, f"{SPEC[name].label}: {'enabled' if enabled else 'disabled'}.")

    @app_commands.command(name="ai-test", description="Test AI providers (all configured ones, or one)")
    @app_commands.guild_only()
    @app_commands.autocomplete(provider=provider_autocomplete)
    async def ai_test(self, interaction: discord.Interaction, provider: Optional[str] = None):
        if not await require(interaction, "admin.ai"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = []
        for p in await self.bot.ai.ordered_providers(interaction.guild_id):
            if provider and p["provider"] != provider:
                continue
            if not p["configured"]:
                if provider:
                    lines.append(f"⚫ **{p['label']}** — no key. `/ai-provider guide name:{p['provider']}`")
                continue
            ok, msg = await self.bot.ai.test_provider(interaction.guild_id, p["provider"])
            lines.append(f"{'🟢' if ok else '🔴'} **{p['label']}** — {E.safe(redact(msg), 250)}")
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🧪 AI Provider Test", look=look, description="\n".join(lines) or (
            "No AI provider is configured. Dispatch still works with local recognition.\nAdd a free key: `/ai-provider list`."))
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="ai-model", description="Choose the model a provider uses on this server")
    @app_commands.guild_only()
    @app_commands.autocomplete(provider=provider_autocomplete, model=model_autocomplete)
    @app_commands.describe(model="Leave empty to go back to the provider's default")
    async def ai_model(self, interaction: discord.Interaction, provider: str, model: Optional[str] = None):
        if not await require(interaction, "admin.ai"):
            return
        if provider not in self.bot.ai.providers:
            return await reply(interaction, "Unknown provider.", error=True)
        value = (model or "").strip()[:100] or None
        await self.bot.ai.set_model(interaction.guild_id, provider, value)
        self.bot.ai.clear_cache()
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.model", provider, {"model": value})
        free_note = ""
        if provider == "openrouter" and value and not (value.endswith(":free") or value == "openrouter/free"):
            free_note = "\n⚠️ This OpenRouter model does not end in `:free` — it will use paid credits."
        await reply(interaction, f"🧩 {SPEC[provider].label} model: `{value or self.bot.ai.providers[provider].default_model}`.{free_note}")

    @app_commands.command(name="ai-failover", description="Turn automatic provider failover on/off and see the order")
    @app_commands.guild_only()
    async def ai_failover(self, interaction: discord.Interaction, enabled: Optional[bool] = None):
        if not await require(interaction, "admin.ai"):
            return
        if enabled is not None:
            await self.bot.ai.update_ai_settings(interaction.guild_id, failover=enabled)
            await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.failover", str(enabled))
        settings = await self.bot.ai.ai_settings(interaction.guild_id)
        order = [p for p in await self.bot.ai.ordered_providers(interaction.guild_id) if p["configured"] and p["enabled"]]
        look = await E.look_for(self.bot, interaction.guild_id)
        e = E.base("🔀 AI Failover", look=look, description=(
            f"Automatic failover is **{'ON' if settings['failover'] else 'OFF'}**. "
            + ("If a provider hits its rate limit or is down, the next one is tried instantly."
               if settings["failover"] else "Only the first provider is tried; if it fails, dispatch uses local recognition.")))
        e.add_field(name="Try order", value="\n".join(f"{i + 1}. {p['label']} — {_state(p)}" for i, p in enumerate(order))
                    or "No provider configured.", inline=False)
        await reply(interaction, embed=e)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(HiveAICog(bot))
