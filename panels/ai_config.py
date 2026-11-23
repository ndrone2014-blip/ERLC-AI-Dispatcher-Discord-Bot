"""Panel 8: AI Configuration."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

import discord

from ai.providers.base import ProviderError
from bot.ui import BRAND, FOOTER, bot_of, reply, require, truncate
from database.models import discord_ts

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def build(bot: "DispatchBot", guild_id: int):
    rows = await bot.ai.guild_providers(guild_id)
    usage = await bot.ai.usage_summary(guild_id)
    e = discord.Embed(title="🧠 AI Configuration", colour=BRAND,
                      description="Providers are tried in order. A failing provider is paused (never hammered) and the next one is used. "
                                  "If none is available, dispatch continues with rule-based assessments and template announcements.")
    for i, p in enumerate(rows):
        role = "Primary" if i == 0 else ("Secondary" if i == 1 else f"Fallback {i}")
        if not p["configured"]:
            state = "⚫ Not configured (add key/URL to .env)"
        elif not p["enabled"]:
            state = "⚪ Disabled"
        elif p["cooling_down"]:
            state = f"🟡 Paused {int(p['cooldown_until'] - time.time())}s"
        else:
            state = "🟢 Ready"
        u = usage.get(p["provider"], {})
        lim = f"/{p['daily_limit']}" if p.get("daily_limit") else ""
        value = (f"{state}\nModel: `{p['effective_model']}`\nToday: {p['requests_today']}{lim} requests"
                 f" · {u.get('failed') or 0} failed · avg {int(u.get('avg_ms') or 0)} ms")
        if p.get("last_error"):
            value += f"\nLast error {discord_ts(p['last_error_at'])}: {truncate(p['last_error'], 120)}"
        e.add_field(name=f"{role}: {p['label']}", value=value, inline=False)
    warnings = await bot.ai.quota_warnings(guild_id)
    if warnings:
        e.add_field(name="⚠️ Quota / availability warnings", value=truncate("\n".join(warnings), 1000), inline=False)
    wording = await bot.core.setting(guild_id, "ai_announcement_wording")
    e.add_field(name="Announcement wording", value="AI-polished (facts verified, template fallback)" if wording else "Templates only",
                inline=True)
    e.add_field(name="Assessment timeout", value=f"{await bot.core.setting(guild_id, 'assessment_timeout_s')} s", inline=True)
    e.set_footer(text=FOOTER)
    return [e], AIView([p["provider"] for p in rows], {p["provider"]: p["label"] for p in rows})


class AIView(discord.ui.View):
    def __init__(self, providers: Optional[list[str]] = None, labels: Optional[dict] = None):
        super().__init__(timeout=None)
        providers = providers or ["gemini", "ollama", "openai_compat"]
        labels = labels or {}
        opts = [discord.SelectOption(label=labels.get(p, p), value=p) for p in providers]
        self.primary.options = opts
        self.secondary.options = [discord.SelectOption(label="(none)", value="none")] + opts

    @discord.ui.select(custom_id="cad:ai:primary", placeholder="Set PRIMARY provider…", row=0)
    async def primary(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        rows = await bot.ai.guild_providers(interaction.guild_id)
        second = rows[1]["provider"] if len(rows) > 1 and rows[1]["provider"] != select.values[0] else rows[0]["provider"]
        await bot.ai.set_order(interaction.guild_id, select.values[0], second)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "ai.primary", select.values[0])
        await reply(interaction, f"Primary AI provider set to **{select.values[0]}**.")

    @discord.ui.select(custom_id="cad:ai:secondary", placeholder="Set SECONDARY (fallback) provider…", row=1)
    async def secondary(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        rows = await bot.ai.guild_providers(interaction.guild_id)
        primary = rows[0]["provider"]
        sec = None if select.values[0] == "none" else select.values[0]
        await bot.ai.set_order(interaction.guild_id, primary, sec)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "ai.secondary", str(sec))
        await reply(interaction, f"Secondary AI provider set to **{sec or 'none'}**.")

    @discord.ui.button(label="Test Providers", emoji="🧪", style=discord.ButtonStyle.primary, custom_id="cad:ai:test", row=2)
    async def test(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = []
        for p in await bot.ai.guild_providers(interaction.guild_id):
            if not p["configured"]:
                lines.append(f"⚫ **{p['label']}** — not configured")
                continue
            ok, msg = await bot.ai.test_provider(interaction.guild_id, p["provider"])
            lines.append(f"{'🟢' if ok else '🔴'} **{p['label']}** — {truncate(msg, 300)}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Choose Model", emoji="🧩", style=discord.ButtonStyle.secondary, custom_id="cad:ai:model", row=2)
    async def model(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        rows = [p for p in await bot.ai.guild_providers(interaction.guild_id) if p["configured"]]
        if not rows:
            return await reply(interaction, "No provider is configured.")
        await reply(interaction, "Which provider's model do you want to change?", view=ModelProviderPick(rows))

    @discord.ui.button(label="Enable / Disable Provider", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="cad:ai:toggle", row=2)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.ai"):
            return
        rows = await bot_of(interaction).ai.guild_providers(interaction.guild_id)
        await reply(interaction, "Toggle a provider:", view=ProviderToggleView(rows))

    @discord.ui.button(label="Toggle AI Announcement Wording", emoji="🗣️", style=discord.ButtonStyle.secondary,
                       custom_id="cad:ai:wording", row=3)
    async def wording(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        cur = await bot.core.setting(interaction.guild_id, "ai_announcement_wording")
        await bot.core.set_setting(interaction.guild_id, "ai_announcement_wording", not cur)
        await reply(interaction, f"AI announcement wording {'enabled' if not cur else 'disabled (templates only)'}.")

    @discord.ui.button(label="Clear Pauses", emoji="♻️", style=discord.ButtonStyle.secondary, custom_id="cad:ai:clear", row=3)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        for p in await bot.ai.guild_providers(interaction.guild_id):
            await bot.ai.clear_cooldown(interaction.guild_id, p["provider"])
        await reply(interaction, "Provider pauses cleared. They will be tried again on the next request.")


class ModelProviderPick(discord.ui.View):
    def __init__(self, rows: list[dict]):
        super().__init__(timeout=120)
        sel = discord.ui.Select(options=[discord.SelectOption(label=r["label"], value=r["provider"]) for r in rows])
        sel.callback = self._pick
        self.add_item(sel)
        self.sel = sel

    async def _pick(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        provider = bot.ai.providers[self.sel.values[0]]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            models = await provider.list_models()
        except ProviderError as e:
            return await interaction.followup.send(f"⚠️ Could not list models: {e}", ephemeral=True)
        await interaction.followup.send(f"Select a model for **{provider.label}**:", view=ModelPick(provider.name, models), ephemeral=True)


class ModelPick(discord.ui.View):
    def __init__(self, provider: str, models: list[str]):
        super().__init__(timeout=120)
        self.provider = provider
        sel = discord.ui.Select(options=[discord.SelectOption(label=m[:100], value=m[:100]) for m in models[:25]])
        sel.callback = self._pick
        self.add_item(sel)
        self.sel = sel

    async def _pick(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        bot = bot_of(interaction)
        await bot.ai.set_model(interaction.guild_id, self.provider, self.sel.values[0])
        await bot.audit.record(interaction.guild_id, interaction.user.id, "ai.model", self.provider, {"model": self.sel.values[0]})
        await interaction.response.edit_message(content=f"Model for {self.provider} set to `{self.sel.values[0]}`.", view=None)


class ProviderToggleView(discord.ui.View):
    def __init__(self, rows: list[dict]):
        super().__init__(timeout=120)
        sel = discord.ui.Select(options=[discord.SelectOption(
            label=f"{'Disable' if r['enabled'] else 'Enable'} {r['label']}", value=f"{r['provider']}:{0 if r['enabled'] else 1}")
            for r in rows])
        sel.callback = self._pick
        self.add_item(sel)
        self.sel = sel

    async def _pick(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        provider, flag = self.sel.values[0].split(":")
        await bot_of(interaction).ai.set_enabled(interaction.guild_id, provider, flag == "1")
        await interaction.response.edit_message(content=f"{provider} {'enabled' if flag == '1' else 'disabled'}.", view=None)
