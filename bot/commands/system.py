"""/voice, /ai, /stats, /erlc, /help commands."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui import reply, require
from panels import ai_config, statistics

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def _provider_ac(interaction: discord.Interaction, current: str):
    from bot.commands.pulse_ai import provider_autocomplete
    return await provider_autocomplete(interaction, current)


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
        if enabled:
            if not self.bot.stt.available():
                return await reply(interaction, f"⚠️ Speech recognition unavailable: {self.bot.stt.last_error}", error=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            ok, msg = await self.bot.voice.enable_listening(interaction.guild_id)
            return await interaction.followup.send(("👂 " if ok else "⚠️ ") + msg, ephemeral=True)
        await self.bot.voice.disable_listening(interaction.guild_id)
        await reply(interaction, "🙉 Listening off.")

    @voice.command(name="diagnose", description="Show why the dispatcher might be silent")
    async def voice_diagnose(self, interaction: discord.Interaction):
        if not await require(interaction, "voice.control"):
            return
        import shutil
        import subprocess
        bot = self.bot
        d = bot.voice.diagnostics(interaction.guild_id)
        s = await bot.voice.settings(interaction.guild_id)
        lines = [f"**Connected:** {d['connected']}" + (f" to `{d['channel']}` with `{d['client']}`" if d["connected"] else "")]
        if d["connected"]:
            dave = d["dave"]
            lines.append(f"**Listeners in channel:** {d['listeners']}")
            lines.append(f"**DAVE encryption:** version {dave['version']} · session {dave['session']} · ready {dave['ready']} · "
                         f"can encrypt {dave['can_encrypt']}")
        lines.append(f"**Speech muted:** {s['muted']} · **Volume:** {s['volume']}")
        ff = bot.settings.ffmpeg_path
        resolved = shutil.which(ff) or ff
        try:
            out = subprocess.run([resolved, "-version"], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        except Exception as e:
            out = f"NOT WORKING ({e})"
        lines.append(f"**FFmpeg:** `{resolved}` — {out[:80]}")
        voices = bot.tts.list_voices()
        lines.append(f"**TTS voices:** {', '.join(voices) or 'none'}" + ("" if any(v != 'windows-sapi' for v in voices)
                                                                       else " (Piper voice missing → Windows voice used)"))
        if bot.tts.last_error:
            lines.append(f"**Last TTS error:** {bot.tts.last_error[:200]}")
        queued = await bot.core.announcements.pending_count(interaction.guild_id)
        lines.append(f"**Queued announcements:** {queued}")
        lines.append(f"**Last playback:** {d['last_playback'] or 'none yet'}")
        lines.append(f"**Listening:** {'on' if s['listening'] else 'OFF'} · **Wake word required:** {s['require_wake_word']}")
        g = bot.voice.gs(interaction.guild_id)
        if g.sink:
            st = g.sink.stats
            lines.append(f"**Receiving:** packets {st.packets} · utterances {st.utterances} · decrypt failures {st.decrypt_failures}")
        if bot.voice_commands is not None:
            lines.append(f"**Speech queue:** {bot.voice_commands.diagnostics_text()}")
        await reply(interaction, "\n".join(lines))

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

    @ai.command(name="primary", description="Set the manual try order: primary and secondary AI provider")
    @app_commands.autocomplete(provider=_provider_ac, secondary=_provider_ac)
    async def ai_primary(self, interaction: discord.Interaction, provider: str, secondary: Optional[str] = None):
        if not await require(interaction, "admin.ai"):
            return
        if provider not in self.bot.ai.providers or (secondary and secondary not in self.bot.ai.providers):
            return await reply(interaction, "Unknown provider — pick one from the list.", error=True)
        await self.bot.ai.set_order(interaction.guild_id, provider, secondary)
        await self.bot.audit.record(interaction.guild_id, interaction.user.id, "ai.primary", provider)
        await reply(interaction, f"Primary AI provider: **{self.bot.ai.providers[provider].label}** (manual order mode).")

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

    @app_commands.command(name="help", description="Pulse Dispatch commands by category")
    async def help(self, interaction: discord.Interaction):
        from bot import embeds as E
        from bot.commands.pulse_info import HelpView, help_embed
        look = await E.look_for(self.bot, interaction.guild_id)
        await reply(interaction, embed=help_embed("home", look), view=HelpView(look))


async def setup(bot: "DispatchBot"):
    await bot.add_cog(SystemCog(bot))
