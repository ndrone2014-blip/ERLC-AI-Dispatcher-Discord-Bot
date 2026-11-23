"""/voice, /ai, /stats, /erlc, /help commands."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui import BRAND, reply, require
from panels import ai_config, statistics

if TYPE_CHECKING:
    from bot.client import DispatchBot


class SystemCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    voice = app_commands.Group(name="voice", description="Dispatcher voice controls", guild_only=True)

    @voice.command(name="join", description="Join the dispatch voice channel")
    async def voice_join(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
        if not await require(interaction, "voice.control"):
            return
        if channel is None:
            s = await self.bot.voice.settings(interaction.guild_id)
            channel = interaction.guild.get_channel(int(s["channel_id"])) if s["channel_id"] else None
            if channel is None and isinstance(interaction.user, discord.Member) and interaction.user.voice:
                channel = interaction.user.voice.channel
        if channel is None:
            return await reply(interaction, "Specify a channel or join one first.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            vc = await self.bot.voice.join(interaction.guild_id, channel.id)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Could not join: {e}", ephemeral=True)
        await interaction.followup.send(f"🎙️ Joined {vc.channel.mention}.", ephemeral=True)

    @voice.command(name="leave", description="Leave the voice channel")
    async def voice_leave(self, interaction: discord.Interaction):
        if not await require(interaction, "voice.control"):
            return
        await self.bot.voice.leave(interaction.guild_id)
        await reply(interaction, "⏏️ Left voice. Queued announcements are preserved.")

    @voice.command(name="listen", description="Turn officer speech recognition on or off")
    async def voice_listen(self, interaction: discord.Interaction, enabled: bool):
        if not await require(interaction, "voice.control"):
            return
        await self.bot.voice.update_settings(interaction.guild_id, listening=enabled)
        if enabled:
            if not self.bot.stt.available():
                return await reply(interaction, f"⚠️ Speech recognition unavailable: {self.bot.stt.last_error}", error=True)
            ok, msg = self.bot.voice.start_listening(interaction.guild_id)
            return await reply(interaction, ("👂 " if ok else "⚠️ ") + msg)
        self.bot.voice.stop_listening(interaction.guild_id)
        await reply(interaction, "🙉 Listening off.")

    @voice.command(name="mute", description="Mute or unmute dispatcher speech (listening is unaffected)")
    async def voice_mute(self, interaction: discord.Interaction, muted: bool):
        if not await require(interaction, "voice.control"):
            return
        await self.bot.voice.update_settings(interaction.guild_id, muted=muted)
        await reply(interaction, "🔇 Muted." if muted else "🔊 Unmuted.")

    @voice.command(name="test", description="Play a radio check")
    async def voice_test(self, interaction: discord.Interaction):
        if not await require(interaction, "voice.control"):
            return
        if self.bot.voice.voice_client(interaction.guild_id) is None:
            return await reply(interaction, "Join a voice channel first.", error=True)
        await self.bot.player.test_voice(interaction.guild_id)
        await reply(interaction, "📢 Radio check queued.")

    ai = app_commands.Group(name="ai", description="AI provider configuration", guild_only=True)

    @ai.command(name="status", description="Show AI provider status and usage")
    async def ai_status(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        embeds, _ = await ai_config.build(self.bot, interaction.guild_id)
        await reply(interaction, embeds=embeds)

    @ai.command(name="test", description="Test every configured AI provider")
    async def ai_test(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.ai"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = []
        for p in await self.bot.ai.guild_providers(interaction.guild_id):
            if not p["configured"]:
                lines.append(f"⚫ {p['label']} — not configured")
                continue
            ok, msg = await self.bot.ai.test_provider(interaction.guild_id, p["provider"])
            lines.append(f"{'🟢' if ok else '🔴'} {p['label']} — {msg[:300]}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @ai.command(name="primary", description="Switch the primary AI provider")
    @app_commands.choices(provider=[app_commands.Choice(name="Google Gemini", value="gemini"),
                                    app_commands.Choice(name="Ollama (local)", value="ollama"),
                                    app_commands.Choice(name="OpenAI-compatible", value="openai_compat")])
    async def ai_primary(self, interaction: discord.Interaction, provider: app_commands.Choice[str],
                         secondary: Optional[str] = None):
        if not await require(interaction, "admin.ai"):
            return
        await self.bot.ai.set_order(interaction.guild_id, provider.value, secondary)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.primary", provider.value)
        await reply(interaction, f"Primary AI provider: **{provider.name}**.")

    @app_commands.command(name="stats", description="Dispatch statistics")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction):
        if not await require(interaction, "stats.view"):
            return
        embeds, _ = await statistics.build(self.bot, interaction.guild_id)
        await reply(interaction, embeds=embeds)

    @app_commands.command(name="erlc", description="Test the ER:LC API connection")
    @app_commands.guild_only()
    async def erlc(self, interaction: discord.Interaction):
        if not await require(interaction, "admin.erlc"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg, _ = await self.bot.sync.test_connection(interaction.guild_id)
        await interaction.followup.send(("🟢 " if ok else "🔴 ") + msg, ephemeral=True)

    @app_commands.command(name="help", description="How to use the dispatch system")
    async def help(self, interaction: discord.Interaction):
        e = discord.Embed(title="📡 ER:LC AI Dispatch — Help", colour=BRAND, description=(
            "**Everything is in the panels** created by `/setup run`.\n\n"
            "**Officers:** register on `#unit-board` (or `/unit register`), set **10-8**, attach to calls from `#active-calls`. "
            "In the 📻 radio channel say *“Dispatch, I'm 10-8”*, *“Dispatch, attach me to incident 104”*, "
            "*“Dispatch, what calls are active?”*. Same in text with `/dispatch ask`.\n"
            "**Citizens:** `#citizen-services` or `/911` (roleplay only — not a real emergency service).\n"
            "**Supervisors:** approve/override AI priorities from alerts in `#department-notices`.\n"
            "**Admins:** `#dispatch-control` → ER:LC key, AI providers, voice, roles, departments, backups, test mode.\n\n"
            "In-game 911 calls are detected automatically through the official ER:LC API and announced in voice."))
        await reply(interaction, embed=e)


async def setup(bot: "DispatchBot"):
    await bot.add_cog(SystemCog(bot))
