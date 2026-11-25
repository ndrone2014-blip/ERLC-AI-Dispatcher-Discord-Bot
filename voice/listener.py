"""Receiving officer speech from Discord voice.

Uses discord-ext-voice-recv to get per-user Opus frames. Since Discord enforces
DAVE end-to-end encryption on voice (from 1 March 2026), each Opus frame is
additionally DAVE-encrypted. voice-recv does not decrypt DAVE itself, so this
sink asks it for raw Opus, decrypts DAVE frames with discord.py's own `davey`
session, and decodes to PCM.

Speaker identity comes ONLY from Discord's SSRC -> user ID mapping, never from
voice characteristics. Bot accounts (including this bot) are ignored, which
prevents the dispatcher from hearing and answering itself.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

try:
    from discord.ext import voice_recv  # type: ignore
    VOICE_RECV_AVAILABLE = True
    _SinkBase = voice_recv.AudioSink
except Exception as _e:  # pragma: no cover - depends on optional package
    voice_recv = None
    VOICE_RECV_AVAILABLE = False
    VOICE_RECV_ERROR = str(_e)
    _SinkBase = object

try:
    import davey  # type: ignore
except Exception:  # pragma: no cover
    davey = None

DAVE_MAGIC = b"\xfa\xfa"
OPUS_SILENCE = b"\xf8\xff\xfe"
MIN_UTTERANCE_S = 0.5
MAX_UTTERANCE_S = 15.0
END_OF_SPEECH_S = 0.5   # silence that ends a transmission (lower = faster replies)
BYTES_PER_SECOND = 48000 * 2 * 2  # 48 kHz, stereo, s16


@dataclass
class _Buffer:
    pcm: bytearray = field(default_factory=bytearray)
    last: float = 0.0
    started: float = 0.0


@dataclass
class ReceiveStats:
    packets: int = 0
    dave_decrypted: int = 0
    passthrough: int = 0
    decrypt_failures: int = 0
    decode_failures: int = 0
    ignored_bots: int = 0
    utterances: int = 0
    last_error: Optional[str] = None


def dave_decrypt(session, user_id: int, frame: bytes, stats: ReceiveStats) -> Optional[bytes]:
    """Decrypt a DAVE-protected Opus frame; plain frames pass through unchanged."""
    if not frame:
        return None
    if session is None or davey is None or not frame.endswith(DAVE_MAGIC):
        stats.passthrough += 1
        return frame
    try:
        out = session.decrypt(user_id, davey.MediaType.audio, frame)
        stats.dave_decrypted += 1
        return out
    except Exception as e:
        stats.decrypt_failures += 1
        stats.last_error = f"DAVE decrypt failed: {e}"
        return None


class DispatchSink(_SinkBase):  # type: ignore[misc]
    def __init__(self, on_utterance: Callable[[int, bytes], None], *, is_paused: Callable[[], bool] = lambda: False,
                 dave_session_getter: Callable[[], object] = lambda: None):
        if VOICE_RECV_AVAILABLE:
            super().__init__()
        self.on_utterance = on_utterance
        self.is_paused = is_paused
        self.dave_session_getter = dave_session_getter
        self.stats = ReceiveStats()
        self._buffers: dict[int, _Buffer] = {}
        self._decoders: dict[int, object] = {}
        self._lock = threading.Lock()
        self._running = True
        self._flusher = threading.Thread(target=self._flush_loop, name="voice-flush", daemon=True)
        self._flusher.start()

    # voice_recv sink API ---------------------------------------------------
    def wants_opus(self) -> bool:
        return True  # we must DAVE-decrypt before decoding

    def write(self, user, data) -> None:
        # This runs on voice-recv's packet thread: an exception escaping here kills that thread and
        # the dispatcher goes deaf for everyone. Never let one bad packet (common in busy calls) do that.
        try:
            self._write(user, data)
        except Exception as e:  # pragma: no cover - defensive
            self.stats.last_error = f"Packet handling failed: {e}"

    def _write(self, user, data) -> None:
        self.stats.packets += 1
        if user is None:
            return
        if getattr(user, "bot", False):
            self.stats.ignored_bots += 1
            return
        if self.is_paused():
            return
        frame = data.opus
        if not frame:
            return
        opus = dave_decrypt(self.dave_session_getter(), user.id, frame, self.stats)
        if opus is None or opus == OPUS_SILENCE:
            return
        try:
            decoder = self._decoders.get(user.id)
            if decoder is None:
                from discord.opus import Decoder
                decoder = Decoder()
                self._decoders[user.id] = decoder
            pcm = decoder.decode(opus, fec=False)
        except Exception as e:
            self.stats.decode_failures += 1
            self.stats.last_error = f"Opus decode failed: {e}"
            return
        now = time.monotonic()
        with self._lock:
            buf = self._buffers.get(user.id)
            if buf is None:
                buf = _Buffer(started=now)
                self._buffers[user.id] = buf
            buf.pcm.extend(pcm)
            buf.last = now
            if len(buf.pcm) >= MAX_UTTERANCE_S * BYTES_PER_SECOND:
                self._emit(user.id, buf)

    def cleanup(self) -> None:
        self._running = False

    # segmentation ------------------------------------------------------------
    def _emit(self, user_id: int, buf: _Buffer) -> None:
        pcm = bytes(buf.pcm)
        self._buffers.pop(user_id, None)
        if len(pcm) >= MIN_UTTERANCE_S * BYTES_PER_SECOND:
            self.stats.utterances += 1
            try:
                self.on_utterance(user_id, pcm)
            except Exception:
                log.exception("Utterance callback failed")

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(0.1)
            now = time.monotonic()
            with self._lock:
                for uid, buf in list(self._buffers.items()):
                    if now - buf.last >= END_OF_SPEECH_S:
                        self._emit(uid, buf)

    def drop_all(self) -> None:
        with self._lock:
            self._buffers.clear()
