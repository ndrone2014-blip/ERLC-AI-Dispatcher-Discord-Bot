"""Regression tests for the 'bot does not talk' fixes."""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from ai.manager import AIManager
from ai.providers.base import AIResponse, ProviderError
from core.events import EventBus
from core.loop_monitor import LoopMonitor
from tests.conftest import GUILD, FakeProvider


class RetiringProvider(FakeProvider):
    """Rejects the retired model like Gemini does, accepts the replacement."""

    async def generate(self, *, system, prompt, model=None, **kw):
        self.calls += 1
        if (model or self.default_model) == "gemini-2.5-flash":
            raise ProviderError("This model models/gemini-2.5-flash is no longer available to new users.", kind="model")
        return AIResponse(text="ok", provider=self.name, model=model, latency_ms=1)

    async def list_models(self):
        return ["gemini-2.5-flash", "gemini-3.8-flash-lite", "gemini-3.8-flash", "gemini-3.9-pro-preview"]


@pytest.mark.asyncio
async def test_retired_model_switches_automatically(db):
    p = RetiringProvider("gemini")
    p.default_model = "gemini-2.5-flash"
    mgr = AIManager(db, EventBus(), [p])
    r = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r.model == "gemini-3.8-flash"
    rows = await mgr.guild_providers(GUILD)
    assert rows[0]["model"] == "gemini-3.8-flash"          # persisted for next time
    r2 = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r2.model == "gemini-3.8-flash" and p.calls == 3  # no repeated failures


def test_gemini_classifies_retired_model():
    from ai.gemini import _error_from
    e = _error_from(400, {"error": {"message": "This model models/gemini-2.5-flash is no longer available to new users."}})
    assert e.kind == "model"
    assert _error_from(404, {"error": {"status": "NOT_FOUND", "message": "x"}}).kind == "model"


@pytest.mark.asyncio
async def test_loop_monitor_reports_blocked_loop(caplog):
    mon = LoopMonitor(threshold=0.3, interval=0.05)
    mon.start()
    await asyncio.sleep(0.2)
    with caplog.at_level(logging.ERROR, logger="loop_monitor"):
        time.sleep(0.8)          # deliberately block the event loop
        await asyncio.sleep(0.3)
    mon.stop()
    assert mon.stalls == 1
    assert any("Event loop blocked" in r.message and "EVENT LOOP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_playback_waits_for_dave_encryption(db, monkeypatch):
    import discord
    from voice.connection import VoiceManager

    conn = SimpleNamespace(dave_protocol_version=1, dave_session=object(), can_encrypt=False)

    class FakeVC:
        _connection = conn
        played = False

        def is_playing(self):
            return False

        def play(self, source, after):
            FakeVC.played = True
            asyncio.get_running_loop().call_later(0.05, after, None)

        def stop(self):
            pass

    loop = asyncio.get_running_loop()
    bot = SimpleNamespace(loop=loop)
    vm = VoiceManager(bot, db, EventBus(), "ffmpeg")
    monkeypatch.setattr(vm, "voice_client", lambda gid: FakeVC())
    monkeypatch.setattr(discord, "FFmpegPCMAudio", lambda *a, **k: object())

    loop.call_later(0.6, lambda: setattr(conn, "can_encrypt", True))   # DAVE becomes ready later
    t0 = time.monotonic()
    await vm.play_file(GUILD, "x.wav", "volume=1")
    assert FakeVC.played and time.monotonic() - t0 >= 0.5               # did not play before encryption was ready
    assert vm.gs(GUILD).last_playback.startswith("OK")


@pytest.mark.asyncio
async def test_playback_refuses_when_dave_never_ready(db, monkeypatch):
    from voice import connection as vconn
    from voice.connection import VoiceManager
    conn = SimpleNamespace(dave_protocol_version=1, dave_session=None, can_encrypt=False)
    vc = SimpleNamespace(_connection=conn, is_playing=lambda: False)
    vm = VoiceManager(SimpleNamespace(loop=asyncio.get_running_loop()), db, EventBus(), "ffmpeg")
    monkeypatch.setattr(vm, "voice_client", lambda gid: vc)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vconn.asyncio, "sleep", lambda s: real_sleep(0))  # skip the 10 s wait
    with pytest.raises(ConnectionError, match="DAVE"):
        await vm.play_file(GUILD, "x.wav", "volume=1")
