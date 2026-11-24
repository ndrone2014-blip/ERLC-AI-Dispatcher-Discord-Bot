"""Panel 4: Voice Control."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.ui import BRAND, FOOTER, bot_of, has_perm, reply, require, truncate
from voice.listener import VOICE_RECV_AVAILABLE

if TYPE_CHECKING:
    from bot.client import DispatchBot


async def build(bot: "DispatchBot", guild_id: int):
    s = await bot.voice.settings(guild_id)
    status = bot.voice.status(guild_id)
    vc = bot.voice.voice_client(guild_id)
    g = bot.voice.gs(guild_id)
    queued = await bot.core.announcements.pending_count(guild_id)
    e = discord.Embed(title="🎙️ Dispatcher Voice Control", colour=BRAND)
    label = {"disconnected": "🔴 Disconnected", "connected": "🟢 Connected (not listening)",
             "listening": "🟢 Connected · 👂 Listening", "speaking": "🔊 Speaking"}[status]
    e.add_field(name="Status", value=label, inline=True)
    configured = bot.get_channel(s["channel_id"]) if s["channel_id"] else None
    e.add_field(name="Channel", value=(vc.channel.mention if vc else (configured.mention + " (not joined)" if configured else "Not set")),
                inline=True)
    e.add_field(name="Auto-reconnect", value="On" if s["autojoin"] else "Off", inline=True)
    e.add_field(name="Dispatcher speech", value="🔇 Muted (text only)" if s["muted"] else "🔊 Enabled", inline=True)
    listen = "On" if s["listening"] else "Off"
    if not VOICE_RECV_AVAILABLE:
        listen = "Unavailable (voice receive library missing)"
    e.add_field(name="Speech recognition", value=listen, inline=True)
    e.add_field(name="Wake word", value="Required (“Dispatch, …”)" if s["require_wake_word"] else "Not required", inline=True)
    voices = bot.tts.list_voices()
    e.add_field(name="Voice", value=f"{s['voice'] or bot.settings.piper_default_voice}" + ("" if voices else " ⚠️ no voices installed"),
                inline=True)
    e.add_field(name="Speed / Volume", value=f"{float(s['speed']):.1f}× / {float(s['volume']):.1f}×", inline=True)
    fx = [n for n, k in (("radio filter", "radio_effect"), ("open beep", "open_beep"), ("close beep", "close_beep"), ("static", "static")) if s[k]]
    e.add_field(name="Radio effects", value=", ".join(fx) or "Off", inline=True)
    e.add_field(name="Announcement queue", value=f"{queued} queued" + (f" · now: {truncate(g.current_announcement['text'], 60)}"
                                                                       if g.current_announcement else ""), inline=False)
    if g.sink:
        st = g.sink.stats
        e.add_field(name="Receive diagnostics",
                    value=(f"packets {st.packets} · DAVE decrypted {st.dave_decrypted} · plain {st.passthrough} · "
                           f"decrypt failures {st.decrypt_failures} · utterances {st.utterances}"
                           + (f"\nLast error: {truncate(st.last_error, 120)}" if st.last_error else "")), inline=False)
    if vc:
        d = bot.voice.dave_state(vc)
        e.add_field(name="Voice encryption (DAVE)",
                    value=("Not used" if not d["version"] else ("🟢 Ready" if d["can_encrypt"] else "🟡 Negotiating — audio waits until ready")),
                    inline=True)
    if g.last_playback:
        e.add_field(name="Last playback", value=truncate(g.last_playback, 200), inline=False)
    if g.last_error and status == "disconnected":
        e.add_field(name="Last voice error", value=truncate(g.last_error, 200), inline=False)
    e.set_footer(text=FOOTER)
    return [e], VoiceView()


class VoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        return await require(interaction, "voice.control")

    @discord.ui.select(cls=discord.ui.ChannelSelect, custom_id="cad:voice:channel", placeholder="Select dispatch voice channel & join…",
                       channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice], row=0)
    async def channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = bot_of(interaction)
        try:
            vc = await bot.voice.join(interaction.guild_id, select.values[0].id)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Could not join: {e}", ephemeral=True)
        await interaction.followup.send(f"🎙️ Joined {vc.channel.mention}.", ephemeral=True)

    @discord.ui.button(label="Join / Reconnect", emoji="🔌", style=discord.ButtonStyle.success, custom_id="cad:voice:join", row=1)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        bot = bot_of(interaction)
        s = await bot.voice.settings(interaction.guild_id)
        if not s["channel_id"]:
            return await reply(interaction, "Pick a voice channel from the menu first.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            vc = bot.voice.voice_client(interaction.guild_id)
            if vc:
                await vc.disconnect(force=True)
            vc = await bot.voice.join(interaction.guild_id, int(s["channel_id"]))
            await interaction.followup.send(f"🔌 Connected to {vc.channel.mention}.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Could not connect: {e}", ephemeral=True)

    @discord.ui.button(label="Leave", emoji="⏏️", style=discord.ButtonStyle.secondary, custom_id="cad:voice:leave", row=1)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await bot_of(interaction).voice.leave(interaction.guild_id)
        await reply(interaction, "⏏️ Left voice. Queued announcements are kept and will play when the dispatcher rejoins.")

    @discord.ui.button(label="Start Listening", emoji="👂", style=discord.ButtonStyle.primary, custom_id="cad:voice:listen", row=1)
    async def listen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        bot = bot_of(interaction)
        if not bot.stt.available():
            return await reply(interaction, f"⚠️ Speech recognition unavailable: {bot.stt.last_error}", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await bot.voice.enable_listening(interaction.guild_id)
        await interaction.followup.send(("👂 " if ok else "⚠️ ") + msg + ("" if ok else " Listening will start automatically once connected."),
                                        ephemeral=True)

    @discord.ui.button(label="Stop Listening", emoji="🙉", style=discord.ButtonStyle.secondary, custom_id="cad:voice:unlisten", row=1)
    async def unlisten(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        bot = bot_of(interaction)
        await bot.voice.disable_listening(interaction.guild_id)
        await reply(interaction, "🙉 Stopped listening. Dispatcher speech is unaffected.")

    @discord.ui.button(label="Mute / Unmute Speech", emoji="🔇", style=discord.ButtonStyle.secondary, custom_id="cad:voice:mute", row=2)
    async def mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        bot = bot_of(interaction)
        s = await bot.voice.settings(interaction.guild_id)
        s = await bot.voice.update_settings(interaction.guild_id, muted=not s["muted"])
        await reply(interaction, "🔇 Dispatcher speech muted (listening unaffected; announcements go to the text log)."
                    if s["muted"] else "🔊 Dispatcher speech enabled.")

    @discord.ui.button(label="Test Voice", emoji="📢", style=discord.ButtonStyle.secondary, custom_id="cad:voice:test", row=2)
    async def test(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        bot = bot_of(interaction)
        if bot.voice.voice_client(interaction.guild_id) is None:
            return await reply(interaction, "Join a voice channel first.")
        await bot.player.test_voice(interaction.guild_id)
        await reply(interaction, "📢 Radio check queued.")

    @discord.ui.button(label="Skip Current", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="cad:voice:skip", row=2)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "announcement.skip_routine"):
            return
        bot = bot_of(interaction)
        ok, msg = await bot.player.skip_current(interaction.guild_id, allow_critical=await has_perm(interaction, "admin.config"))
        await reply(interaction, ("⏭️ " if ok else "⚠️ ") + msg)

    @discord.ui.button(label="Voice Settings", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="cad:voice:settings", row=2)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        bot = bot_of(interaction)
        await reply(interaction, "Adjust the dispatcher voice:", view=VoiceSettingsView(bot.tts.list_voices()))


class VoiceSettingsView(discord.ui.View):
    def __init__(self, voices: list[str]):
        super().__init__(timeout=300)
        if voices:
            sel = discord.ui.Select(placeholder="Change dispatcher voice…", row=0,
                                    options=[discord.SelectOption(label=v[:100], value=v[:100]) for v in voices[:25]])
            sel.callback = self._voice
            self.add_item(sel)
            self.voice_sel = sel

    async def _voice(self, interaction: discord.Interaction):
        if not await require(interaction, "voice.control"):
            return
        await bot_of(interaction).voice.update_settings(interaction.guild_id, voice=self.voice_sel.values[0])
        await reply(interaction, f"Voice set to `{self.voice_sel.values[0]}`.")

    async def _adjust(self, interaction: discord.Interaction, key: str, delta: float, lo: float, hi: float):
        if not await require(interaction, "voice.control"):
            return
        bot = bot_of(interaction)
        s = await bot.voice.settings(interaction.guild_id)
        val = round(min(hi, max(lo, float(s[key]) + delta)), 1)
        await bot.voice.update_settings(interaction.guild_id, **{key: val})
        await reply(interaction, f"{key.title()} set to {val:.1f}×.")

    async def _toggle(self, interaction: discord.Interaction, key: str):
        if not await require(interaction, "voice.control"):
            return
        bot = bot_of(interaction)
        s = await bot.voice.settings(interaction.guild_id)
        s = await bot.voice.update_settings(interaction.guild_id, **{key: not s[key]})
        await reply(interaction, f"{key.replace('_', ' ').title()}: {'On' if s[key] else 'Off'}.")

    @discord.ui.button(label="Slower", emoji="🐢", row=1)
    async def slower(self, i, b):
        await self._adjust(i, "speed", -0.1, 0.6, 1.6)

    @discord.ui.button(label="Faster", emoji="🐇", row=1)
    async def faster(self, i, b):
        await self._adjust(i, "speed", 0.1, 0.6, 1.6)

    @discord.ui.button(label="Quieter", emoji="🔉", row=1)
    async def quieter(self, i, b):
        await self._adjust(i, "volume", -0.1, 0.2, 2.0)

    @discord.ui.button(label="Louder", emoji="🔊", row=1)
    async def louder(self, i, b):
        await self._adjust(i, "volume", 0.1, 0.2, 2.0)

    @discord.ui.button(label="Radio filter", row=2)
    async def radio(self, i, b):
        await self._toggle(i, "radio_effect")

    @discord.ui.button(label="Open beep", row=2)
    async def ob(self, i, b):
        await self._toggle(i, "open_beep")

    @discord.ui.button(label="Close beep", row=2)
    async def cb(self, i, b):
        await self._toggle(i, "close_beep")

    @discord.ui.button(label="Static", row=2)
    async def st(self, i, b):
        await self._toggle(i, "static")

    @discord.ui.button(label="Wake word", row=3)
    async def ww(self, i, b):
        await self._toggle(i, "require_wake_word")

    @discord.ui.button(label="Half-duplex (echo guard)", row=3)
    async def hd(self, i, b):
        await self._toggle(i, "half_duplex")
