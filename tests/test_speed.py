"""Speed behaviour: never wait on slow AI, cheap wake-word pass, no stale backlog."""
import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ai.manager import AIManager
from ai.providers.base import AIProvider, AIResponse
from core.events import EventBus
from database.models import DEFAULT_STATUS_CODES
from tests.conftest import GUILD, settle
from voice.commands import VoiceCommandProcessor

CODES = [{"code": c, "label": l, "availability": a, "sort": s} for c, l, a, s in DEFAULT_STATUS_CODES]


class SlowProvider(AIProvider):
    name = "gemini"
    label = "slow"

    def __init__(self):
        super().__init__("m")
        self.timeouts = []

    @property
    def configured(self):
        return True

    async def generate(self, *, system, prompt, model=None, json_mode=False, max_tokens=600, temperature=0.3, timeout=20.0):
        self.timeouts.append(timeout)
        await asyncio.sleep(30)
        return AIResponse(text="{}", provider="gemini", model="m")


@pytest.mark.asyncio
async def test_new_911_is_queued_without_waiting_for_slow_ai(core, db):
    slow = SlowProvider()
    core.ai = AIManager(db, EventBus(), [slow])
    t0 = time.monotonic()
    inc = await core.create_test_call(GUILD, 1, location="Main St", description="car crash with injuries")
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"911 pipeline waited {elapsed:.1f}s for AI"
    ann = await core.announcements.next_queued(GUILD)
    assert ann and ann["incident_id"] == inc["id"] and "Main St" in ann["text"]
    assert inc["assessment_source"] == "rules"
    await asyncio.sleep(0.05)
    assert slow.timeouts and slow.timeouts[0] <= 6.0   # background AI assessment uses a short time limit


@pytest.mark.asyncio
async def test_intent_ai_timeout_is_short(db):
    slow = SlowProvider()
    mgr = AIManager(db, EventBus(), [slow])
    task = asyncio.create_task(mgr.generate(GUILD, purpose="intent", system="s", prompt="p"))
    await asyncio.sleep(0.05)
    task.cancel()
    assert slow.timeouts == [4.0]


class FakeSTT:
    def __init__(self, quick_text, full_text):
        self.quick_text, self.full_text = quick_text, full_text
        self.quick_calls = self.full_calls = 0
        self.has_quick_model = True

    async def quick_transcribe(self, audio, max_seconds=None):
        self.quick_calls += 1
        return self.quick_text

    async def transcribe(self, audio):
        self.full_calls += 1
        return self.full_text

    def available(self):
        return False


def processor(core, stt):
    voice = SimpleNamespace(settings=lambda gid: asyncio.sleep(0, {"listening": True, "require_wake_word": True}))
    return VoiceCommandProcessor(core, voice, stt, None, None, None)


@pytest.mark.asyncio
async def test_chatter_is_dropped_after_quick_pass(core):
    stt = FakeSTT("so anyway we were driving down the road", "unused")
    p = processor(core, stt)
    audio = np.zeros(16000 * 3, dtype=np.float32)
    text, woke, rest, intent = await p._transcribe(audio, CODES, True)
    assert not woke and stt.quick_calls == 1 and stt.full_calls == 0


@pytest.mark.asyncio
async def test_clear_command_uses_quick_pass_only(core):
    stt = FakeSTT("Dispatch, show me available.", "unused")
    p = processor(core, stt)
    text, woke, rest, intent = await p._transcribe(np.zeros(16000 * 2, dtype=np.float32), CODES, True)
    assert woke and intent.name == "status_change" and stt.full_calls == 0


@pytest.mark.asyncio
async def test_unclear_quick_pass_falls_back_to_accurate_model(core):
    stt = FakeSTT("Dispatch, hmm something mumbled", "Dispatch, show me on a traffic stop at Main Street")
    p = processor(core, stt)
    text, woke, rest, intent = await p._transcribe(np.zeros(16000 * 3, dtype=np.float32), CODES, True)
    assert stt.full_calls == 1 and intent.name == "self_initiated"


@pytest.mark.parametrize("quick", [
    "Dispatch, show me available", "this patch show me available", "Dispatched, attach me to the call",
    "Despatch, traffic stop", "The spatch, I'm on scene", "Dispatcher, radio check", "dispatch's show me 10-8",
    "Disbatch, show me available", "Just patch attach me",
])
def test_lenient_wake_on_fast_transcript(quick):
    from ai.conversation import maybe_wake_word
    assert maybe_wake_word(quick)


@pytest.mark.parametrize("chatter", [
    "so anyway we were driving down the road", "the district attorney said no", "this is crazy man",
    "look at the display", "yeah I'll be there in a minute", "",
])
def test_chatter_not_mistaken_for_wake(chatter):
    from ai.conversation import maybe_wake_word, strip_wake_word
    assert not maybe_wake_word(chatter)
    assert not strip_wake_word(chatter)[0]


@pytest.mark.parametrize("heard,rest", [
    ("Dispatched, show me available.", "show me available"),
    ("Disbatch show me on a traffic stop", "show me on a traffic stop"),
    ("Despatch, I'm on scene", "i'm on scene"),
])
def test_fuzzy_wake_on_accurate_transcript(heard, rest):
    from ai.conversation import strip_wake_word
    woke, r = strip_wake_word(heard)
    assert woke and r == rest


def test_radio_check_is_built_in():
    from ai.conversation import parse_rules, strip_wake_word
    for heard in ("Dispatch, are you there?", "Dispatch, radio check", "Dispatch, do you copy?"):
        assert parse_rules(strip_wake_word(heard)[1], CODES).name == "radio_check"


@pytest.mark.asyncio
async def test_possible_wake_on_fast_pass_uses_accurate_model(core):
    stt = FakeSTT("Just patch attach me to the coal", "Dispatch, attach me to the call.")
    p = processor(core, stt)
    text, woke, rest, intent = await p._transcribe(np.zeros(16000 * 2, dtype=np.float32), CODES, True)
    assert stt.full_calls == 1 and woke and intent.name == "attach"


@pytest.mark.asyncio
async def test_stale_clips_are_dropped(core):
    handled = []
    p = processor(core, FakeSTT("", ""))

    async def fake_handle(gid, uid, pcm):
        handled.append(uid)

    p._handle = fake_handle
    p._queue.put_nowait((GUILD, 1, b"", time.monotonic() - 30))   # waited 30 s -> outdated
    p._queue.put_nowait((GUILD, 2, b"", time.monotonic()))
    task = asyncio.create_task(p._worker())
    await asyncio.sleep(0.05)
    task.cancel()
    assert handled == [2]
