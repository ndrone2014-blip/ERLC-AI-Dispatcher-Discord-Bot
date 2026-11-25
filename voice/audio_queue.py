"""Plays queued announcements in voice, highest priority first.

The queue itself lives in the database (dispatch/announcements.py), so nothing is
lost if the bot restarts or voice disconnects: items simply stay 'queued' until a
voice connection is available. When the dispatcher is muted, announcements are
marked 'muted' (they still appear in the Discord dispatch log).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from core.events import Event, EventBus
from dispatch.announcements import ANNOUNCEMENT_UPDATED, AnnouncementService

from collections import OrderedDict

from .audio_processing import build_transmission, ffmpeg_filter, render_pcm
from .connection import VoiceManager
from .text_to_speech import TTSEngine, TTSError

log = logging.getLogger(__name__)

PREEMPT_BELOW = 40  # a critical announcement may cut off a routine one below this priority


class AnnouncementPlayer:
    def __init__(self, voice: VoiceManager, tts: TTSEngine, announcements: AnnouncementService, bus: EventBus):
        self.voice = voice
        self.tts = tts
        self.announcements = announcements
        self.bus = bus
        self._wake: dict[int, asyncio.Event] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._preempted: set[int] = set()
        self._pcm_cache: "OrderedDict[tuple, bytes]" = OrderedDict()   # finished transmissions, ready to play
        bus.subscribe(ANNOUNCEMENT_UPDATED, self._on_update)

    def start_guild(self, guild_id: int) -> None:
        if guild_id in self._tasks and not self._tasks[guild_id].done():
            return
        self._wake[guild_id] = asyncio.Event()
        self._tasks[guild_id] = asyncio.create_task(self._loop(guild_id), name=f"announce-{guild_id}")

    async def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _on_update(self, e: Event) -> None:
        if e.data.get("status") != "queued":
            return
        w = self._wake.get(e.guild_id)
        if w:
            w.set()
        # Pre-empt a routine announcement for a critical one.
        g = self.voice.gs(e.guild_id)
        cur = g.current_announcement
        if cur and g.speaking and cur["priority"] < PREEMPT_BELOW:
            nxt = await self.announcements.next_queued(e.guild_id)
            if nxt and nxt["critical"]:
                log.info("Pre-empting routine announcement %s for critical %s", cur["id"], nxt["id"])
                self._preempted.add(cur["id"])
                self.voice.interrupt(e.guild_id)

    async def skip_current(self, guild_id: int, allow_critical: bool = False) -> tuple[bool, str]:
        g = self.voice.gs(guild_id)
        cur = g.current_announcement
        if not cur or not g.speaking:
            return False, "Nothing is being announced right now."
        if cur["critical"] and not allow_critical:
            return False, "Critical announcements can only be interrupted by an administrator."
        self.voice.interrupt(guild_id)
        await self.announcements.mark(cur["id"], "cancelled", "skipped by supervisor")
        return True, "Announcement interrupted."

    async def _loop(self, guild_id: int) -> None:
        wake = self._wake[guild_id]
        while True:
            try:
                await self._drain(guild_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Announcement loop error for guild %s", guild_id)
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def _drain(self, guild_id: int) -> None:
        while True:
            ann = await self.announcements.next_queued(guild_id)
            if ann is None:
                return
            settings = await self.voice.settings(guild_id)
            if settings["muted"]:
                await self.announcements.mark(ann["id"], "muted", "dispatcher speech muted")
                continue
            if self.voice.voice_client(guild_id) is None:
                return  # preserved in the queue until voice is available
            await self._play(guild_id, ann, settings)

    async def _play(self, guild_id: int, ann: dict, settings: dict) -> None:
        g = self.voice.gs(guild_id)
        await self.announcements.mark(ann["id"], "playing")
        speech = tx = None
        try:
            radio = await self.voice.radio_settings(guild_id)
            key = (ann["text"], settings.get("voice"), float(settings.get("speed", 1.0)), radio.radio_effect,
                   radio.open_beep, radio.close_beep, radio.static, float(radio.volume), radio.pause_ms)
            pcm = self._pcm_cache.get(key)
            if pcm is None:
                speech = await self.tts.synthesize(ann["text"], settings.get("voice"), float(settings.get("speed", 1.0)))
                try:
                    pcm = await asyncio.to_thread(render_pcm, speech, radio, ann["id"])
                    self._pcm_cache[key] = pcm
                    while len(self._pcm_cache) > 100:
                        self._pcm_cache.popitem(last=False)
                except Exception:
                    log.warning("In-memory radio render failed; using FFmpeg", exc_info=True)
                    pcm = None
            else:
                self._pcm_cache.move_to_end(key)
            if pcm is None:
                tx = speech.with_name(speech.stem + "-tx.wav")
                await asyncio.to_thread(build_transmission, speech, tx, radio, ann["id"])
            g.current_announcement = ann
            g.last_spoken_text = ann["text"]
            g.speaking = True
            await self.bus.publish("voice.status", guild_id)
            if pcm is not None:
                await self.voice.play_pcm(guild_id, pcm)
            else:
                await self.voice.play_file(guild_id, str(tx), ffmpeg_filter(radio))
            if ann["id"] in self._preempted:
                self._preempted.discard(ann["id"])
                await self.announcements.mark(ann["id"], "queued", "pre-empted by critical announcement")
            else:
                row = await self.announcements.db.fetchone("SELECT status FROM announcements WHERE id = ?", (ann["id"],))
                if row and row["status"] == "playing":
                    await self.announcements.mark(ann["id"], "played")
        except TTSError as e:
            log.error("TTS failed for announcement %s: %s", ann["id"], e)
            await self.announcements.mark(ann["id"], "failed", f"TTS: {e}"[:300])
        except Exception as e:
            log.warning("Playback failed for announcement %s: %s", ann["id"], e)
            await self.announcements.mark(ann["id"], "failed", str(e)[:300])
            await asyncio.sleep(1)
        finally:
            if g.speaking:
                g.last_spoken_end = time.monotonic()
            g.speaking = False
            g.current_announcement = None
            await self.bus.publish("voice.status", guild_id)
            if speech:
                self.tts.cleanup(speech)
            if tx:
                self.tts.cleanup(tx)

    async def test_voice(self, guild_id: int, text: Optional[str] = None) -> Optional[int]:
        return await self.announcements.enqueue(
            guild_id, "test", text or "Dispatch radio check. This is a test of the dispatcher voice system. Dispatch clear.",
            dedupe_key=f"voicetest:{asyncio.get_running_loop().time():.3f}", priority=90)
