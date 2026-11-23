import asyncio
import os
import shutil
import subprocess
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import GUILD, settle
from voice.audio_processing import (RadioSettings, build_transmission, ffmpeg_filter, pcm48k_stereo_to_16k_mono, read_wav_mono,
                                    write_wav_mono)
from voice.listener import DAVE_MAGIC, DispatchSink, ReceiveStats, dave_decrypt
from voice.text_to_speech import TTSEngine, TTSError, spoken_form

FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


def _tone_wav(path, seconds=1.0, sr=22050):
    t = np.arange(int(sr * seconds)) / sr
    write_wav_mono(path, 0.3 * np.sin(2 * np.pi * 440 * t), sr)


def test_transmission_adds_beeps_and_pauses(tmp_path):
    src, out = tmp_path / "s.wav", tmp_path / "o.wav"
    _tone_wav(src, 1.0)
    build_transmission(src, out, RadioSettings())
    a, sr = read_wav_mono(src)
    b, sr2 = read_wav_mono(out)
    assert sr == sr2 and len(b) > len(a) + sr * 0.3
    build_transmission(src, out, RadioSettings(open_beep=False, close_beep=False, static=False))
    c, _ = read_wav_mono(out)
    assert len(c) < len(b)


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg not installed")
@pytest.mark.parametrize("radio", [True, False])
def test_ffmpeg_filter_chain_is_valid(tmp_path, radio):
    src, out = tmp_path / "s.wav", tmp_path / "o.wav"
    _tone_wav(src, 0.5)
    tx = build_transmission(src, out, RadioSettings(radio_effect=radio))
    # Same decode Discord playback uses: 48 kHz stereo s16le PCM
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(tx), "-af", ffmpeg_filter(RadioSettings(radio_effect=radio)),
                           "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    assert len(proc.stdout) > 48000 * 2 * 2 * 0.5


def test_pcm_conversion():
    stereo = (np.ones(48000 * 2, dtype=np.int16) * 1000).tobytes()  # 1 s of 48 kHz stereo
    mono = pcm48k_stereo_to_16k_mono(stereo)
    assert mono.dtype == np.float32 and abs(len(mono) - 16000) <= 1


def test_spoken_form():
    assert spoken_form("Unit 1A-12 is 10-8, see #104, EMS") == "Unit 1A-12 is ten 8, see number 104, E M S"


class FakeSession:
    def __init__(self):
        self.calls = []

    def decrypt(self, user_id, media_type, frame):
        self.calls.append(user_id)
        return frame[:-2].replace(b"ENC", b"")


def test_dave_decrypt_paths():
    st = ReceiveStats()
    assert dave_decrypt(None, 1, b"plain", st) == b"plain" and st.passthrough == 1
    sess = FakeSession()
    assert dave_decrypt(sess, 7, b"ENCopus" + DAVE_MAGIC, st) == b"opus"
    assert st.dave_decrypted == 1 and sess.calls == [7]

    class Bad:
        def decrypt(self, *a):
            raise RuntimeError("no key")
    assert dave_decrypt(Bad(), 7, b"x" + DAVE_MAGIC, st) is None and st.decrypt_failures == 1


def _opus_frames(seconds: float):
    from discord.opus import Encoder, _load_default
    _load_default()
    enc = Encoder()
    sr, frame = 48000, 960
    t = np.arange(int(sr * seconds)) / sr
    pcm = (0.3 * np.sin(2 * np.pi * 300 * t) * 32767).astype(np.int16)
    stereo = np.repeat(pcm, 2)
    frames = []
    for i in range(0, len(pcm) - frame, frame):
        frames.append(enc.encode(stereo[i * 2:(i + frame) * 2].tobytes(), frame))
    return frames


def test_sink_segments_utterances_and_ignores_bots():
    try:
        frames = _opus_frames(1.2)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Opus library unavailable: {e}")
    got = []
    paused = {"v": False}
    sink = DispatchSink(lambda uid, pcm: got.append((uid, len(pcm))), is_paused=lambda: paused["v"])
    officer = SimpleNamespace(id=42, bot=False)
    botuser = SimpleNamespace(id=99, bot=True)
    for f in frames:
        sink.write(officer, SimpleNamespace(opus=f))
        sink.write(botuser, SimpleNamespace(opus=f))
    time.sleep(1.4)
    assert got and got[0][0] == 42
    assert got[0][1] >= 48000 * 4 * 1.0       # ~1.2 s of 48 kHz stereo s16
    assert all(uid != 99 for uid, _ in got)   # never hears bots (incl. itself)
    assert sink.stats.ignored_bots == len(frames)
    # half-duplex echo guard: nothing captured while paused
    got.clear()
    paused["v"] = True
    for f in frames:
        sink.write(officer, SimpleNamespace(opus=f))
    time.sleep(1.2)
    assert got == []
    sink.cleanup()


def test_sapi_tts_fallback(tmp_path):
    tts = TTSEngine(tmp_path / "voices", "missing-voice", tmp_path)
    try:
        path = asyncio.run(tts.synthesize("Dispatch radio check, ten 8.", voice="windows-sapi"))
    except TTSError as e:
        pytest.skip(f"Windows SAPI unavailable here: {e}")
    with wave.open(str(path), "rb") as w:
        assert w.getnframes() > 1000


@pytest.mark.skipif(not os.environ.get("PIPER_TEST_VOICE"), reason="set PIPER_TEST_VOICE=path\\to\\voice.onnx to run")
def test_piper_tts(tmp_path):
    from pathlib import Path
    voice = Path(os.environ["PIPER_TEST_VOICE"])
    tts = TTSEngine(voice.parent, voice.stem, tmp_path)
    path = asyncio.run(tts.synthesize("Attention all units, new 911 call."))
    with wave.open(str(path), "rb") as w:
        assert w.getnframes() > 5000


@pytest.mark.skipif(not os.environ.get("RUN_STT_TEST"), reason="downloads the Whisper model; set RUN_STT_TEST=1 to run")
def test_whisper_transcribes_tts(tmp_path):
    from voice.speech_to_text import STTEngine
    tts = TTSEngine(tmp_path / "v", "x", tmp_path)
    wav = asyncio.run(tts.synthesize("Dispatch, attach me to incident one oh four.", voice="windows-sapi"))
    audio, sr = read_wav_mono(wav)
    idx = (np.arange(int(len(audio) * 16000 / sr)) * sr / 16000).astype(int)
    text = asyncio.run(STTEngine("tiny.en").transcribe(audio[idx].astype(np.float32)))
    assert "dispatch" in text.lower()


@pytest.mark.asyncio
async def test_announcement_player_queue_behaviour(core, db, tmp_path):
    from voice.audio_queue import AnnouncementPlayer

    played = []

    class FakeVoice:
        connected = False
        muted = False

        def gs(self, gid):
            return self._g

        async def settings(self, gid):
            return {"muted": self.muted, "voice": None, "speed": 1.0}

        async def radio_settings(self, gid):
            return RadioSettings()

        def voice_client(self, gid):
            return object() if self.connected else None

        async def play_file(self, gid, path, filt):
            played.append(path)

        def interrupt(self, gid):
            return True

    fv = FakeVoice()
    fv._g = SimpleNamespace(speaking=False, current_announcement=None)

    class FakeTTS:
        async def synthesize(self, text, voice=None, speed=1.0):
            p = tmp_path / f"t{len(played)}.wav"
            _tone_wav(p, 0.2)
            return p

        def cleanup(self, *p):
            pass

    player = AnnouncementPlayer(fv, FakeTTS(), core.announcements, core.bus)
    inc = await core.create_test_call(GUILD, 1, location="Main", description="crash")
    await settle()
    # Not connected: stays queued (never discarded)
    await player._drain(GUILD)
    assert (await core.announcements.next_queued(GUILD))["status"] == "queued"
    # Connected: plays and is recorded
    fv.connected = True
    await player._drain(GUILD)
    assert len(played) == 1
    row = await db.fetchone("SELECT status, played_at FROM announcements WHERE incident_id = ?", (inc["id"],))
    assert row["status"] == "played" and row["played_at"]
    # Muted: consumed as text-only
    fv.muted = True
    await core.department_notice(GUILD, 1, "Shift change at 2200")
    await player._drain(GUILD)
    assert await db.fetchval("SELECT status FROM announcements WHERE kind = 'department_notice'") == "muted"
    assert len(played) == 1


@pytest.mark.asyncio
async def test_priority_ordering(core, db):
    await core.announcements.enqueue(GUILD, "department_notice", "routine", dedupe_key="a")
    await core.announcements.enqueue(GUILD, "new_911", "crit", dedupe_key="b", incident_priority="critical")
    await core.announcements.enqueue(GUILD, "bolo", "bolo", dedupe_key="c")
    first = await core.announcements.next_queued(GUILD)
    assert first["text"] == "crit" and first["critical"] == 1
