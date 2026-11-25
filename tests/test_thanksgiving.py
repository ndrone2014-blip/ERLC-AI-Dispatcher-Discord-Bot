"""Thanksgiving Update: natural-language recognition, duplicate merging, follow-ups, AI providers, isolation."""
import asyncio
import time

import pytest

from ai.conversation import ConversationMemory, Intent, complete_pending, understand
from ai.manager import AIManager, AIUnavailable
from ai.providers.base import ProviderError
from ai.providers.catalog import CATALOG, SPEC, looks_like_key
from core.events import EventBus
from core.logging_setup import redact
from database.models import DEFAULT_STATUS_CODES
from dispatch import nlu, phrases
from dispatch.recommendations import rule_based_assessment
from tests.conftest import GUILD, FakeProvider, settle

CODES = [{"code": c, "label": lbl} for c, lbl, _, _ in DEFAULT_STATUS_CODES]

SAME_INCIDENT = [
    "We have a vehicle crash at the bank.",
    "At the bank there's a crash involving a vehicle.",
    "Vehicle accident, bank location.",
    "Send police to the bank, there's been an accident.",
    "Need EMS at the bank for a crash.",
]


# ---------------------------------------------------------------------------- local recognition
@pytest.mark.parametrize("text", SAME_INCIDENT)
def test_word_order_independent(text):
    a = nlu.analyze(text)
    assert a.category == "Traffic collision", text
    assert a.location == "Bank", (text, a.location)
    assert a.is_emergency_report


@pytest.mark.parametrize("text,category,location", [
    ("theres a car acident @ the gas stattion pls hurry", "Traffic collision", "Gas Station"),
    ("mva w/ injuries on main street p214", "Traffic collision", "Main Street Postal 214"),
    ("someone is robbing the jewlery store", "Major robbery", "Jewelry Store"),
    ("shots fired downtown bank", "Shots fired / shooting", "Downtown Bank"),
    ("open fire at the park", "Shots fired / shooting", "Park"),
    ("house fire on 5th avenue", "Fire", "5th Avenue"),
    ("two car pileup on the highway", "Multi-vehicle collision", "Highway"),
    ("guy passed out outside the hospital", "Medical emergency", "Hospital"),
    ("10-50 at postal 312", "Traffic collision", "Postal 312"),
    ("the old warehouse on 5th street is on fire", "Fire", "Old Warehouse on 5th Street"),
])
def test_spelling_slang_codes_and_locations(text, category, location):
    a = nlu.analyze(text)
    assert a.category == category, (text, a.category)
    assert a.location == location, (text, a.location)


def test_department_requests_and_injuries():
    assert "ems" in nlu.analyze("Need EMS at the bank for a crash").requested_kinds
    assert "law" in nlu.analyze("send police to the gas station").requested_kinds
    assert "fire" not in nlu.analyze("there's a fire at the bank").requested_kinds   # bare "fire" is the incident
    a = nlu.analyze("crash at the mall, someone is bleeding")
    assert a.injuries and "ems" in a.kinds()
    r = rule_based_assessment("crash at the mall, someone is bleeding")
    assert r["priority"] == "high" and "ems" in r["department_kinds"]


def test_follow_up_questions():
    a = nlu.analyze("there's a crash")
    assert a.missing == ["location"] and "location" in a.follow_up().lower()
    i = complete_pending({"text": "there's a crash", "nature": "Vehicle collision"}, "main street")
    assert i.name == "report_incident" and i.args["location"] == "Main Street" and not i.args["missing"]


@pytest.mark.parametrize("text,intent", [
    ("available show me", "status_change"),
    ("show me 10-8", "status_change"),
    ("me attach incident 104 to", "attach"),
    ("attatch me to incident 12", "attach"),
    ("units available who", "query_available"),
    ("what calls are active", "query_active"),
    ("I need backup", "backup"),
    ("show me on a traffic stop at the bank", "self_initiated"),
    ("need units at the bank theres a robbery", "report_incident"),
    ("Need EMS at the bank for a crash.", "report_incident"),
])
def test_understand_officer_requests_vs_reports(text, intent):
    got = understand(text, CODES)
    assert got is not None and got.name == intent, (text, got)


def test_local_recognition_is_fast():
    started = time.perf_counter()
    for i in range(200):
        nlu.analyze(f"we have a vehicle crash at the bank number {i}, need ems")
    assert (time.perf_counter() - started) / 200 < 0.02  # well under 20 ms each (typically < 1 ms)


def test_location_matching():
    assert nlu.locations_match("Downtown Bank", "the bank")
    assert nlu.locations_match("Main Street", "Main Street Postal 214")
    assert not nlu.locations_match("Park", "Trailer Park")
    assert not nlu.locations_match("Bank", "Gas Station")


def test_phrases_keep_facts_and_vary():
    inc = {"number": 42, "location": "Downtown Bank", "nature": "Vehicle collision", "priority": "high", "source": "discord",
           "verification": "unverified", "recommended_departments": '["PD", "EMS"]', "guild_id": 7}
    seen = set()
    for _ in range(12):
        t = phrases.new_call_broadcast(inc, guild_id=7)
        assert "Downtown Bank" in t and "42" in t and "Unverified" in t and "EMS" in t
        seen.add(t)
    assert len(seen) > 1, "dispatcher wording should vary"


# ---------------------------------------------------------------------------- incidents
@pytest.mark.asyncio
async def test_duplicate_reports_merge_into_one_incident(core):
    first, merged = await core.report_incident(GUILD, 10, text=SAME_INCIDENT[0], caller="A")
    assert not merged
    for n, text in enumerate(SAME_INCIDENT[1:]):
        inc, merged = await core.report_incident(GUILD, 20 + n, text=text, caller=f"C{n}")
        assert merged and inc["id"] == first["id"], text
    await settle()
    assert len(await core.incidents.list_active(GUILD)) == 1
    assert len(await core.incidents.notes(first["id"])) == 4


@pytest.mark.asyncio
async def test_different_events_are_not_merged(core):
    a, _ = await core.report_incident(GUILD, 1, text="crash at the bank", caller="A")
    b, merged = await core.report_incident(GUILD, 2, text="fight at the bank", caller="B")
    c, merged2 = await core.report_incident(GUILD, 3, text="crash at the gas station", caller="C")
    assert not merged and not merged2 and len({a["id"], b["id"], c["id"]}) == 3


@pytest.mark.asyncio
async def test_servers_are_isolated(core):
    a, _ = await core.report_incident(GUILD, 1, text="crash at the bank", caller="A")
    await core.units.ensure_defaults(GUILD + 1, "Other")
    b, merged = await core.report_incident(GUILD + 1, 1, text="crash at the bank", caller="B")
    assert not merged and b["guild_id"] == GUILD + 1 and b["number"] == 1
    await core.set_setting(GUILD, "response_style", "concise")
    assert await core.setting(GUILD + 1, "response_style") == "professional"


@pytest.mark.asyncio
async def test_custom_categories(core):
    custom = [{"keywords": ["turf war"], "category": "Gang activity", "priority": "high", "units": 3, "kinds": ["law"]}]
    r = rule_based_assessment("there is a turf war at the trailer park", custom=custom)
    assert r["category"] == "Gang activity" and r["priority"] == "high" and r["recommended_units"] == 3


@pytest.mark.asyncio
async def test_new_status_codes_added_once(core):
    codes = {c["code"] for c in await core.units.status_codes(GUILD)}
    assert {"10-6", "10-19"} <= codes
    await core.units.remove_status_code(GUILD, "10-19", 1)
    await core.units.ensure_defaults(GUILD, "Test Guild")
    assert "10-19" not in {c["code"] for c in await core.units.status_codes(GUILD)}  # admin's removal is respected


# ---------------------------------------------------------------------------- AI providers
@pytest.mark.asyncio
async def test_failover_modes_and_cache(db):
    limited = FakeProvider("groq", error=ProviderError("slow down", kind="quota", retry_after=60))
    good = FakeProvider("openrouter", replies=["one", "two", "three"])
    mgr = AIManager(db, EventBus(), [limited, good])
    r = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r.provider == "openrouter" and mgr.active_provider(GUILD)["provider"] == "openrouter"
    await mgr.clear_cooldown(GUILD, "groq")
    await mgr.update_ai_settings(GUILD, failover=False)
    with pytest.raises(AIUnavailable):
        await mgr.generate(GUILD, purpose="t", system="s", prompt="p2")
    await mgr.update_ai_settings(GUILD, failover=True, preferred="openrouter")
    assert (await mgr.ordered_providers(GUILD))[0]["provider"] == "openrouter"
    calls = good.calls
    await mgr.generate(GUILD, purpose="intent", system="s", prompt="same")
    await mgr.generate(GUILD, purpose="intent", system="s", prompt="same")
    assert good.calls == calls + 1  # second identical request came from the cache


@pytest.mark.asyncio
async def test_unexpected_provider_exception_fails_over(db):
    class Broken(FakeProvider):
        async def generate(self, **kw):
            raise ValueError("not json")
    mgr = AIManager(db, EventBus(), [Broken("groq"), FakeProvider("openrouter", replies=["ok"])])
    r = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r.provider == "openrouter"


@pytest.mark.asyncio
async def test_new_providers_appended_after_existing_order(db):
    mgr = AIManager(db, EventBus(), [FakeProvider("gemini"), FakeProvider("ollama")])
    await mgr.set_order(GUILD, "ollama", "gemini")
    mgr2 = AIManager(db, EventBus(), [FakeProvider("groq"), FakeProvider("gemini"), FakeProvider("ollama")])
    order = [p["provider"] for p in await mgr2.guild_providers(GUILD)]
    assert order == ["ollama", "gemini", "groq"]


def test_catalog_is_honest_and_complete():
    names = {s.name for s in CATALOG}
    for required in ("gemini", "openrouter", "groq", "cerebras", "mistral", "huggingface", "cloudflare", "sambanova", "nvidia", "ollama"):
        assert required in names
    for s in CATALOG:
        assert s.cost in {"free", "credits", "trial", "paid", "local"}
        assert s.signup_url.startswith("http")
        if s.cost == "paid":
            assert "free" not in s.free_tier.lower() or "not free" in s.free_tier.lower()
    assert SPEC["huggingface"].cost == "credits"   # tiny monthly credit is not a free tier
    assert SPEC["openrouter"].default_model in {"openrouter/free"} or SPEC["openrouter"].default_model.endswith(":free")
    assert looks_like_key("groq", "sk-wrong-prefix-1234567890")
    assert looks_like_key("groq", "gsk_" + "a" * 40) is None


def test_redaction_never_leaks_keys():
    for key in ("sk-or-v1-" + "a" * 40, "gsk_" + "b" * 40, "AIza" + "c" * 35, "nvapi-" + "d" * 40, "hf_" + "e" * 30):
        assert key not in redact(f"Error 401: invalid key {key} supplied")
    assert "secretvalue" not in redact("Authorization: Bearer secretvalue1234")


@pytest.mark.asyncio
async def test_pending_report_memory():
    m = ConversationMemory()
    m.set_pending(1, 2, {"text": "there's a crash", "nature": "Vehicle collision"})
    assert m.has_pending(1, 2) and not m.has_pending(1, 3) and not m.has_pending(9, 2)
    assert m.pop_pending(1, 2)["nature"] == "Vehicle collision"
    assert m.pop_pending(1, 2) is None
    assert isinstance(Intent("x"), Intent)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reasoning_model_empty_reply_is_retried():
    """Groq's gpt-oss models think first; a small max_tokens used to return an empty reply."""
    from ai.providers.openai_compat import OpenAICompatProvider
    sent = []

    class Resp:
        def __init__(self, d):
            self.d, self.status, self.headers = d, 200, {}

        async def json(self, content_type=None):
            return self.d

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def post(self, url, json, headers, timeout):
            sent.append(dict(json))
            if json["max_tokens"] < 2048:
                return Resp({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
            return Resp({"choices": [{"message": {"content": "DISPATCH ONLINE"}, "finish_reason": "stop"}]})

    p = OpenAICompatProvider("https://api.groq.com/openai/v1", "gsk_" + "x" * 40, "openai/gpt-oss-20b", lambda: Session(),
                             name="groq", needs_key=True)
    r = await p.generate(system="s", prompt="Reply with exactly: DISPATCH ONLINE", max_tokens=40)
    assert r.text == "DISPATCH ONLINE"
    assert sent[0]["max_tokens"] >= 1024 and sent[0].get("reasoning_effort") == "low"


class _GemResp:
    def __init__(self, status, d):
        self.status, self.d, self.headers = status, d, {}

    async def json(self, content_type=None):
        return self.d

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _gemini_session(behaviour, calls):
    class Session:
        def post(self, url, json, headers, timeout):
            model = url.split("/models/")[1].split(":")[0]
            calls.append(model)
            return behaviour(model, json)
    return Session()


@pytest.mark.asyncio
async def test_gemini_overloaded_model_falls_back_to_another_free_model():
    from ai.gemini import GeminiProvider
    busy = {"error": {"code": 503, "status": "UNAVAILABLE",
                      "message": "This model is currently experiencing high demand. Please try again later."}}
    ok = {"candidates": [{"content": {"parts": [{"text": "DISPATCH ONLINE"}]}, "finishReason": "STOP"}]}
    calls = []

    def behaviour(model, body):
        if model == "gemini-3.8-flash":
            return _GemResp(503, busy)
        if model == "gemini-3.1-flash-lite":
            return _GemResp(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "not found"}})
        return _GemResp(200, ok)

    p = GeminiProvider("AIza" + "x" * 35, "gemini-3.8-flash", lambda: _gemini_session(behaviour, calls))
    r = await p.generate(system="s", prompt="p", max_tokens=40)
    assert r.text == "DISPATCH ONLINE" and r.model == "gemini-3.5-flash-lite"
    assert calls == ["gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]


@pytest.mark.asyncio
async def test_gemini_auth_error_is_not_retried_and_all_busy_reports_unavailable():
    from ai.gemini import GeminiProvider
    calls = []
    bad_key = lambda m, b: _GemResp(400, {"error": {"code": 400, "message": "API key not valid"}})  # noqa: E731
    p = GeminiProvider("AIza" + "x" * 35, "gemini-3.8-flash", lambda: _gemini_session(bad_key, calls))
    with pytest.raises(ProviderError) as e:
        await p.generate(system="s", prompt="p")
    assert e.value.kind == "auth" and calls == ["gemini-3.8-flash"]
    calls.clear()
    busy = lambda m, b: _GemResp(503, {"error": {"code": 503, "message": "high demand"}})  # noqa: E731
    p = GeminiProvider("AIza" + "x" * 35, "gemini-3.8-flash", lambda: _gemini_session(busy, calls))
    with pytest.raises(ProviderError) as e:
        await p.generate(system="s", prompt="p")
    assert e.value.kind == "unavailable" and len(calls) == 3
