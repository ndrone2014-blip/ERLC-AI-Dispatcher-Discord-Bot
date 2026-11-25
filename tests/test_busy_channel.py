"""Crowded voice channels: officers must still get answers when lots of people are talking."""
import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import GUILD
from voice.commands import VoiceCommandProcessor, looks_like_echo


class FakeSTT:
    has_quick_model = True

    def __init__(self, quick_text="", full_text=""):
        self.quick_text, self.full_text = quick_text, full_text
        self.windows = []

    async def quick_transcribe(self, audio, max_seconds=None):
        self.windows.append(max_seconds)
        return self.quick_text

    async def transcribe(self, audio):
        return self.full_text

    def available(self):
        return False


def processor(core, stt=None):
    voice = SimpleNamespace(settings=lambda gid: asyncio.sleep(0, {"listening": True, "require_wake_word": True,
                                                                    "half_duplex": True}))
    return VoiceCommandProcessor(core, voice, stt or FakeSTT(), None, None, None)


def test_echo_of_dispatcher_is_recognised():
    spoken = "1A-12, copy, attached to incident 104, Main Street bank."
    assert looks_like_echo("1A-12 copy attached to incident 104 Main Street bank", spoken)
    assert looks_like_echo("copy attached to incident 104", spoken)


def test_officer_talking_over_dispatcher_is_not_echo():
    spoken = "Attention all units, new 911 call, vehicle crash at the gas station."
    assert not looks_like_echo("Dispatch, show me 10-8", spoken)
    assert not looks_like_echo("Dispatch, attach me to the call", spoken)
    assert not looks_like_echo("ok", spoken)


@pytest.mark.asyncio
async def test_one_speaker_cannot_fill_the_queue(core):
    p = processor(core)
    for _ in range(10):
        p.submit(GUILD, 1, b"x")
    p.submit(GUILD, 2, b"x")
    assert p._queue.qsize() == p.PER_USER_MAX + 1
    assert p.dropped["per_user"] == 10 - p.PER_USER_MAX


@pytest.mark.asyncio
async def test_registered_officers_are_transcribed_first(core):
    p = processor(core)
    p._officers[GUILD] = (time.monotonic(), {7})
    p.submit(GUILD, 1, b"civ")
    p.submit(GUILD, 2, b"civ")
    p.submit(GUILD, 7, b"officer")
    handled = []

    async def fake_handle(gid, uid, pcm, echo_text=None):
        handled.append(uid)

    p._handle = fake_handle
    task = asyncio.create_task(p._worker())
    await asyncio.sleep(0.05)
    task.cancel()
    assert handled == [7, 1, 2]


@pytest.mark.asyncio
async def test_long_chatter_only_screens_the_start(core):
    stt = FakeSTT("so anyway we were driving down the road")
    p = processor(core, stt)
    await p._transcribe(np.zeros(16000 * 5, dtype=np.float32), [], True)
    assert stt.windows == [None]              # quiet channel: one full pass, no extra pass for commands
    stt.windows.clear()
    p.submit(GUILD, 1, b"x")
    p.submit(GUILD, 2, b"x")
    await p._transcribe(np.zeros(16000 * 5, dtype=np.float32), [], True)
    assert stt.windows == [p.SCREEN_S]        # busy channel: chatter screened on its start only


@pytest.mark.asyncio
async def test_echo_clip_is_ignored(core):
    replies = []
    p = processor(core, FakeSTT("Dispatch copies loud and clear, go ahead.", "Dispatch copies loud and clear, go ahead."))
    p.member_lookup = lambda gid, uid: SimpleNamespace(bot=False, display_name="Officer")

    async def fake_reply(*a):
        replies.append(a)

    p._reply = fake_reply
    loud = (np.ones(48000 * 2 * 2, dtype=np.int16) * 3000).tobytes()   # 1 s of non-silent audio
    await p._handle(GUILD, 5, loud, echo_text="Dispatch copies loud and clear, go ahead.")
    assert replies == [] and p.dropped["echo"] == 1


def test_radio_transmission_renders_to_discord_pcm_in_memory(tmp_path):
    from voice.audio_processing import FRAME_BYTES, RadioSettings, render_pcm, write_wav_mono
    sr = 22050
    t = np.arange(sr) / sr
    wav = tmp_path / "speech.wav"
    write_wav_mono(wav, 0.5 * np.sin(2 * np.pi * 440 * t), sr)
    for fx in (True, False):
        pcm = render_pcm(wav, RadioSettings(radio_effect=fx))
        assert len(pcm) % FRAME_BYTES == 0
        assert 1.0 < len(pcm) / (48000 * 4) < 2.0          # ~1 s of speech plus beeps, 48 kHz stereo s16
        assert np.abs(np.frombuffer(pcm, np.int16)).max() > 3000


@pytest.mark.asyncio
async def test_wake_word_alone_then_command_is_answered(core):
    stt = FakeSTT()
    p = processor(core, stt)
    p.GO_AHEAD_DELAY_S = 0.05
    stt.quick_text = "Dispatch."
    text, woke, rest, intent = await p._transcribe(np.zeros(16000, dtype=np.float32), [], True)
    assert woke and rest == "" and intent is None and stt.windows == [None]   # no slow second pass
