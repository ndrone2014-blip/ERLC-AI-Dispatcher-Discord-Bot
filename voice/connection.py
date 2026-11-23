"""Discord voice connection management with persistence and automatic reconnects."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

import discord

from core import events as ev
from core.events import EventBus
from database.connection import Database

from .listener import VOICE_RECV_AVAILABLE, DispatchSink, voice_recv

if TYPE_CHECKING:
    from .audio_processing import RadioSettings

log = logging.getLogger(__name__)

DEFAULT_VOICE_SETTINGS = {
    "channel_id": None,
    "autojoin": False,
    "listening": False,
    "muted": False,
    "voice": None,
    "speed": 1.0,
    "volume": 1.0,
    "radio_effect": True,
    "open_beep": True,
    "close_beep": True,
    "static": True,
    "require_wake_word": True,
    "half_duplex": True,
}


@dataclass
class GuildVoice:
    guild_id: int
    speaking: bool = False
    current_announcement: Optional[dict] = None
    sink: Optional[DispatchSink] = None
    reconnect_failures: int = 0
    last_error: Optional[str] = None


class VoiceManager:
    def __init__(self, bot: discord.Client, db: Database, bus: EventBus, ffmpeg_path: str):
        self.bot = bot
        self.db = db
        self.bus = bus
        self.ffmpeg_path = ffmpeg_path
        self.state: dict[int, GuildVoice] = {}
        self.on_utterance: Optional[Callable[[int, int, bytes], None]] = None  # (guild, user, pcm)
        self._watchdog: Optional[asyncio.Task] = None
        self._join_locks: dict[int, asyncio.Lock] = {}
        self._half_duplex_cache: dict[int, bool] = {}

    def gs(self, guild_id: int) -> GuildVoice:
        if guild_id not in self.state:
            self.state[guild_id] = GuildVoice(guild_id)
        return self.state[guild_id]

    async def settings(self, guild_id: int) -> dict:
        stored = await self.db.get_config(guild_id, "voice", {}) or {}
        return {**DEFAULT_VOICE_SETTINGS, **stored}

    async def update_settings(self, guild_id: int, **changes) -> dict:
        s = await self.settings(guild_id)
        s.update({k: v for k, v in changes.items() if k in DEFAULT_VOICE_SETTINGS})
        await self.db.set_config(guild_id, "voice", s)
        await self.bus.publish(ev.VOICE_STATUS, guild_id)
        return s

    async def radio_settings(self, guild_id: int) -> "RadioSettings":
        from .audio_processing import RadioSettings
        s = await self.settings(guild_id)
        return RadioSettings(radio_effect=s["radio_effect"], open_beep=s["open_beep"], close_beep=s["close_beep"],
                             static=s["static"], volume=float(s["volume"]))

    def voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        return vc if isinstance(vc, discord.VoiceClient) and vc.is_connected() else None

    def status(self, guild_id: int) -> str:
        vc = self.voice_client(guild_id)
        g = self.gs(guild_id)
        if vc is None:
            return "disconnected"
        if g.speaking:
            return "speaking"
        if hasattr(vc, "is_listening") and vc.is_listening():
            return "listening"
        return "connected"

    # ------------------------------------------------------------------ connect
    async def join(self, guild_id: int, channel_id: int) -> discord.VoiceClient:
        lock = self._join_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise ValueError("That voice channel no longer exists.")
            vc = self.voice_client(guild_id)
            if vc and vc.channel.id == channel_id:
                return vc
            if vc:
                await vc.move_to(channel)
            else:
                cls = voice_recv.VoiceRecvClient if VOICE_RECV_AVAILABLE else discord.VoiceClient
                vc = await channel.connect(cls=cls, self_deaf=False, timeout=20, reconnect=True)
            await self.update_settings(guild_id, channel_id=channel_id, autojoin=True)
            self.gs(guild_id).reconnect_failures = 0
            settings = await self.settings(guild_id)
            if settings["listening"]:
                self.start_listening(guild_id)
            await self.bus.publish(ev.VOICE_STATUS, guild_id)
            await self.bus.publish(ev.ACTIVITY, guild_id, text=f"Dispatcher joined voice channel {channel.name}")
            return vc

    async def leave(self, guild_id: int) -> None:
        vc = self.voice_client(guild_id)
        self.stop_listening(guild_id, persist=False)
        await self.update_settings(guild_id, autojoin=False)
        if vc:
            await vc.disconnect(force=True)
        await self.bus.publish(ev.VOICE_STATUS, guild_id)

    # ------------------------------------------------------------------ listening
    def listening_supported(self) -> tuple[bool, str]:
        if not VOICE_RECV_AVAILABLE:
            return False, "discord-ext-voice-recv is not installed; speech recognition is unavailable."
        return True, "ok"

    def start_listening(self, guild_id: int) -> tuple[bool, str]:
        ok, msg = self.listening_supported()
        if not ok:
            return False, msg
        vc = self.voice_client(guild_id)
        if vc is None:
            return False, "Not connected to voice."
        if not hasattr(vc, "listen"):
            return False, "Connected with a client that cannot receive audio. Rejoin the channel."
        if vc.is_listening():
            return True, "Already listening."
        g = self.gs(guild_id)

        def on_utt(user_id: int, pcm: bytes) -> None:
            if self.on_utterance:
                self.bot.loop.call_soon_threadsafe(self.on_utterance, guild_id, user_id, pcm)

        def paused() -> bool:
            # Half-duplex echo prevention: ignore input while the dispatcher is transmitting.
            return g.speaking and self._half_duplex_cache.get(guild_id, True)

        def dave_session():
            conn = getattr(vc, "_connection", None)
            if conn is None or not getattr(conn, "dave_protocol_version", 0):
                return None
            sess = getattr(conn, "dave_session", None)
            return sess if sess is not None and getattr(sess, "ready", False) else None

        g.sink = DispatchSink(on_utt, is_paused=paused, dave_session_getter=dave_session)
        vc.listen(g.sink, after=lambda err: log.warning("Voice listening stopped: %s", err) if err else None)
        return True, "Listening for officer transmissions."

    def stop_listening(self, guild_id: int, persist: bool = True) -> None:
        vc = self.voice_client(guild_id)
        if vc and hasattr(vc, "is_listening") and vc.is_listening():
            vc.stop_listening()
        g = self.gs(guild_id)
        if g.sink:
            g.sink.cleanup()
        g.sink = None

    # ------------------------------------------------------------------ playback
    async def play_file(self, guild_id: int, path: str, filter_chain: str, timeout: float = 120) -> None:
        vc = self.voice_client(guild_id)
        if vc is None:
            raise ConnectionError("Not connected to voice")
        done = asyncio.get_running_loop().create_future()

        def after(err):
            if not done.done():
                self.bot.loop.call_soon_threadsafe(
                    lambda: done.set_exception(err) if err else done.set_result(None))

        source = discord.FFmpegPCMAudio(path, executable=self.ffmpeg_path, options=f'-af "{filter_chain}"')
        if vc.is_playing():
            vc.stop()
        vc.play(source, after=after)
        try:
            await asyncio.wait_for(done, timeout=timeout)
        finally:
            if vc.is_playing():
                vc.stop()

    def interrupt(self, guild_id: int) -> bool:
        vc = self.voice_client(guild_id)
        if vc and vc.is_playing():
            vc.stop()
            return True
        return False

    # ------------------------------------------------------------------ watchdog
    def start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch(), name="voice-watchdog")

    async def _watch(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            for guild in list(self.bot.guilds):
                try:
                    s = await self.settings(guild.id)
                    self._half_duplex_cache[guild.id] = bool(s.get("half_duplex", True))
                    if not s["autojoin"] or not s["channel_id"]:
                        continue
                    vc = self.voice_client(guild.id)
                    g = self.gs(guild.id)
                    if vc is None:
                        backoff = min(300, 10 * (2 ** min(g.reconnect_failures, 5)))
                        g.reconnect_failures += 1
                        try:
                            await self.join(guild.id, int(s["channel_id"]))
                            log.info("Voice reconnected for guild %s", guild.id)
                            await self.bus.publish(ev.ACTIVITY, guild.id, text="Dispatcher voice reconnected")
                        except Exception as e:
                            g.last_error = str(e)
                            log.warning("Voice reconnect failed for guild %s: %s (next try in ~%ss)", guild.id, e, backoff)
                            await asyncio.sleep(min(backoff, 60))
                    elif s["listening"] and hasattr(vc, "is_listening") and not vc.is_listening():
                        self.start_listening(guild.id)
                except Exception:
                    log.exception("Voice watchdog error for guild %s", guild.id)
            await asyncio.sleep(10)

    async def shutdown(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        for guild in list(self.bot.guilds):
            vc = self.voice_client(guild.id)
            if vc:
                self.stop_listening(guild.id, persist=False)
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
