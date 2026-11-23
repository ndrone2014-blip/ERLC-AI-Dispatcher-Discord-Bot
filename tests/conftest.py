import asyncio
import sys
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.providers.base import AIProvider, AIResponse, ProviderError  # noqa: E402
from core.events import EventBus  # noqa: E402
from database.connection import Database  # noqa: E402
from dispatch.core import DispatchCore  # noqa: E402
from security.audit import AuditLog  # noqa: E402

GUILD = 1111


class FakeProvider(AIProvider):
    """Scriptable AI provider for tests (no network)."""

    def __init__(self, name: str, replies=None, error: ProviderError | None = None):
        super().__init__("fake-model")
        self.name = name
        self.label = name
        self.replies = list(replies or [])
        self.error = error
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def generate(self, *, system, prompt, model=None, json_mode=False, max_tokens=600, temperature=0.3, timeout=20.0):
        self.calls += 1
        if self.error:
            raise self.error
        text = self.replies.pop(0) if self.replies else "{}"
        if callable(text):
            text = text(prompt)
        return AIResponse(text=text, provider=self.name, model="fake-model", latency_ms=1)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def core(db):
    bus = EventBus()
    c = DispatchCore(db, bus, AuditLog(db), ai=None)
    await c.units.ensure_defaults(GUILD, "Test Guild")
    yield c
    await asyncio.sleep(0.05)  # let event handlers finish


async def settle():
    """Event handlers run as tasks; give them a moment."""
    for _ in range(5):
        await asyncio.sleep(0.02)
