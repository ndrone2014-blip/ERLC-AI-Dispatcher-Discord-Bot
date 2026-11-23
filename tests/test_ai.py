import json

import pytest

from ai.assessment import _contains_facts, announcement_for_new_call, assess_incident
from ai.conversation import normalize_speech, parse_rules, parse_with_ai, strip_wake_word
from ai.manager import AIManager, AIUnavailable
from ai.providers.base import ProviderError, extract_json
from core.events import EventBus
from database.models import DEFAULT_STATUS_CODES
from dispatch.recommendations import normalize_assessment, rule_based_assessment
from tests.conftest import GUILD, FakeProvider

DEPTS = [
    {"short_code": "PD", "name": "Police Department", "kind": "law", "active": 1},
    {"short_code": "FD", "name": "Fire Department", "kind": "fire", "active": 1},
    {"short_code": "EMS", "name": "EMS", "kind": "ems", "active": 1},
    {"short_code": "SUP", "name": "Dispatch Supervisors", "kind": "supervisor", "active": 1},
]
CODES = [{"code": c, "label": l, "availability": a, "sort": s} for c, l, a, s in DEFAULT_STATUS_CODES]


@pytest.mark.asyncio
async def test_fallback_to_secondary_and_cooldown(db):
    primary = FakeProvider("gemini", error=ProviderError("quota exceeded", kind="quota", retry_after=60))
    secondary = FakeProvider("ollama", replies=["hello", "again"])
    mgr = AIManager(db, EventBus(), [primary, secondary])
    r = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r.provider == "ollama" and primary.calls == 1
    r = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r.provider == "ollama"
    assert primary.calls == 1  # cooling down: NOT retried
    warnings = await mgr.quota_warnings(GUILD)
    assert any("paused" in w for w in warnings)


@pytest.mark.asyncio
async def test_all_providers_down_raises(db):
    mgr = AIManager(db, EventBus(), [FakeProvider("gemini", error=ProviderError("down", kind="unavailable"))])
    with pytest.raises(AIUnavailable):
        await mgr.generate(GUILD, purpose="t", system="s", prompt="p")


@pytest.mark.asyncio
async def test_daily_limit_respected(db):
    p = FakeProvider("gemini", replies=["a", "b", "c"])
    mgr = AIManager(db, EventBus(), [p], daily_limits={"gemini": 2})
    await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    with pytest.raises(AIUnavailable, match="daily limit"):
        await mgr.generate(GUILD, purpose="t", system="s", prompt="p")


@pytest.mark.asyncio
async def test_provider_order_switching(db):
    a, b = FakeProvider("gemini", replies=["a"]), FakeProvider("ollama", replies=["b"])
    mgr = AIManager(db, EventBus(), [a, b])
    await mgr.set_order(GUILD, "ollama", "gemini")
    r = await mgr.generate(GUILD, purpose="t", system="s", prompt="p")
    assert r.provider == "ollama"
    assert await mgr.primary_name(GUILD) == "ollama"


def test_normalize_never_invents_departments():
    raw = {"priority": "HIGH", "recommended_units": 50, "departments": ["PD", "SWAT", "Coast Guard", "EMS"],
           "supervisor_notification": True, "reasoning": "x"}
    out = normalize_assessment(raw, DEPTS, assigned_count=1, available_by_kind={"law": 0})
    assert out["priority"] == "high"
    assert out["recommended_units"] == 10
    assert out["additional_units"] == 9
    assert "SWAT" not in out["departments"] and "Coast Guard" not in out["departments"]
    assert set(out["departments"]) == {"PD", "EMS", "SUP"}
    assert any("No law-enforcement units" in n for n in out["availability_notes"])


def test_rule_assessment_examples():
    minor = rule_based_assessment("fender bender minor traffic accident")
    assert minor["recommended_units"] == 1 and minor["priority"] == "medium"
    multi = rule_based_assessment("multiple vehicle crash on the highway")
    assert multi["recommended_units"] >= 2 and {"fire", "ems"} <= set(multi["department_kinds"])
    shooting = rule_based_assessment("shots fired near the bank")
    assert shooting["priority"] == "critical" and shooting["supervisor_notification"]
    unclear = rule_based_assessment("asdkjh")
    assert unclear["uncertain"]


@pytest.mark.asyncio
async def test_ai_assessment_used_and_validated(db):
    reply = json.dumps({"category": "Collision", "priority": "medium", "recommended_units": 2,
                        "department_kinds": ["law"], "departments": ["PD", "ImaginaryDept"], "supervisor_notification": False,
                        "uncertain": False, "reasoning": "Two vehicles."})
    mgr = AIManager(db, EventBus(), [FakeProvider("gemini", replies=[reply])])
    inc = {"id": 1, "number": 7, "source": "erlc", "verification": "verified", "nature": "crash", "description": "crash",
           "location": "Main St"}
    out = await assess_incident(mgr, GUILD, inc, departments=DEPTS, assigned_units=[], availability={}, notes=[])
    assert out["source"] == "ai:gemini" and out["departments"] == ["PD"]


@pytest.mark.asyncio
async def test_ai_assessment_bad_json_falls_back_to_rules(db):
    mgr = AIManager(db, EventBus(), [FakeProvider("gemini", replies=["not json at all"])])
    inc = {"id": 1, "number": 7, "source": "erlc", "verification": "verified", "nature": "house fire", "description": None,
           "location": "Main St"}
    out = await assess_incident(mgr, GUILD, inc, departments=DEPTS, assigned_units=[], availability={}, notes=[])
    assert out["source"] == "rules" and "FD" in out["departments"]


@pytest.mark.asyncio
async def test_announcement_wording_must_preserve_facts(db):
    inc = {"number": 12, "source": "erlc", "verification": "verified", "location": "Park Street near Postal 218",
           "nature": "Vehicle accident", "description": "Vehicle accident"}
    liar = FakeProvider("gemini", replies=["Attention units, incident 12 at Elm Road, vehicle accident."])
    mgr = AIManager(db, EventBus(), [liar])
    text, source = await announcement_for_new_call(mgr, GUILD, inc)
    assert source == "template" and "Park Street near Postal 218" in text
    good = FakeProvider("gemini", replies=["All units, incident 12, Park Street near Postal 218, reported vehicle accident."])
    mgr2 = AIManager(db, EventBus(), [good])
    text, source = await announcement_for_new_call(mgr2, GUILD, inc)
    assert source == "ai:gemini"


def test_contains_facts_requires_test_label():
    inc = {"number": 3, "source": "test", "verification": "test", "location": "Main"}
    assert not _contains_facts("incident 3 at main", inc)
    assert _contains_facts("test call incident 3 at main", inc)


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ProviderError):
        extract_json("nothing")


@pytest.mark.parametrize("text,intent,args", [
    ("Dispatch, I'm 10-8", "status_change", {"code": "10-8"}),
    ("dispatch show me ten seven", "status_change", {"code": "10-7"}),
    ("dispatch 10 23", "status_change", {"code": "10-23"}),
    ("Dispatch, attach me to incident 104", "attach", {"incident_number": 104}),
    ("dispatch I'm clear from that incident", "detach", {}),
    ("dispatch request backup", "backup", None),
    ("dispatch what's the highest priority call", "query_highest", {}),
    ("dispatch what calls are active", "query_active", {}),
    ("dispatch how many units are assigned to the current incident", "query_incident_units", {}),
    ("dispatch show me available units", "query_available", {}),
])
def test_rule_intents(text, intent, args):
    woke, rest = strip_wake_word(text)
    assert woke
    got = parse_rules(rest, CODES)
    assert got is not None and got.name == intent
    if args is not None:
        for k, v in args.items():
            assert got.args.get(k) == v


def test_incident_number_not_mangled_into_ten_code():
    assert "incident 104" in normalize_speech("attach me to incident 104")
    assert normalize_speech("show me 108") == "show me 10-8"


@pytest.mark.asyncio
async def test_ai_intent_cannot_invent_incident_number(db):
    reply = json.dumps({"intent": "attach", "args": {"incident_number": 999}, "confidence": 0.9})
    mgr = AIManager(db, EventBus(), [FakeProvider("gemini", replies=[reply])])
    intent = await parse_with_ai(mgr, GUILD, "put me on that robbery call", CODES, [], None)
    assert intent.name == "attach" and "incident_number" not in intent.args
