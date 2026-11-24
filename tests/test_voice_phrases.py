"""Natural phrases officers say on the radio, including typical speech-to-text mishearings."""
import pytest

from ai.conversation import parse_rules, strip_wake_word
from database.models import DEFAULT_STATUS_CODES
from tests.conftest import GUILD, settle
from voice.commands import execute_intent

CODES = [{"code": c, "label": l, "availability": a, "sort": s} for c, l, a, s in DEFAULT_STATUS_CODES]


@pytest.mark.parametrize("heard,intent,args", [
    ("Dispatch, attach me to the call.", "attach", {"latest": True}),
    ("Dispatch attachee me to the call", "attach", {"latest": True}),
    ("This patch, attach me to the call.", "attach", {"latest": True}),
    ("Dispatch, attack me to that call", "attach", {"latest": True}),
    ("Dispatch, put me on that call", "attach", {"latest": True}),
    ("Dispatch, show me en route to the call", "attach", {"latest": True}),
    ("Dispatch, attach me to incident 7", "attach", {"incident_number": 7}),
    ("Dispatch show me available", "status_change", {"code": "10-8"}),
    ("Dispatch, show me avable.", "status_change", {"code": "10-8"}),
    ("Dispatcher, I'm available", "status_change", {"code": "10-8"}),
    ("Dispatch, show me ten eight", "status_change", {"code": "10-8"}),
    ("Dispatch show me 10 7", "status_change", {"code": "10-7"}),
    ("Dispatch, I'm on scene", "status_change", {"code": "10-23"}),
    ("Dispatch, show me available units", "query_available", {}),
    ("Dispatch, I'm clear from that call", "detach", {}),
])
def test_phrases(heard, intent, args):
    woke, rest = strip_wake_word(heard)
    assert woke, heard
    got = parse_rules(rest, CODES)
    assert got is not None and got.name == intent, (heard, got)
    for k, v in args.items():
        assert got.args.get(k) == v, (heard, got.args)
    assert got.clarification is None


def test_no_wake_word_is_ignored():
    woke, _ = strip_wake_word("attach me to the call")
    assert not woke


@pytest.mark.asyncio
async def test_attach_to_the_call_uses_latest_announced(core, db):
    officer = await core.units.register(GUILD, 50, "Officer", "2B-20", "PD")
    await core.assignments.set_unit_status(officer["unit_id"], "10-8", 50)
    first = await core.create_test_call(GUILD, 1, location="Main St", description="theft")
    second = await core.create_test_call(GUILD, 1, location="Park St", description="car crash")
    await settle()
    # Only the second call has been announced out loud
    ann = await db.fetchone("SELECT id FROM announcements WHERE incident_id = ? AND kind = 'new_911'", (second["id"],))
    await core.announcements.mark(ann["id"], "played")
    woke, rest = strip_wake_word("Dispatch, attachee me to the call")
    officer = await core.units.officer_by_discord(GUILD, 50)
    reply = await execute_intent(core, GUILD, officer, parse_rules(rest, CODES), 50)
    assert f"incident {second['number']}" in reply and "Park St" in reply
    unit = await core.units.unit(officer["unit_id"])
    assert unit["current_incident_id"] == second["id"]       # confirmed only after the DB recorded it
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_attach_to_the_call_asks_when_ambiguous(core):
    officer = await core.units.register(GUILD, 51, "Officer", "2B-21", "PD")
    a = await core.create_test_call(GUILD, 1, location="A", description="theft")
    b = await core.create_test_call(GUILD, 1, location="B", description="theft")
    _, rest = strip_wake_word("Dispatch attach me to the call")
    reply = await execute_intent(core, GUILD, officer, parse_rules(rest, CODES), 51)
    assert "Which incident" in reply and str(a["number"]) in reply and str(b["number"]) in reply


@pytest.mark.asyncio
async def test_attach_single_active_call(core):
    officer = await core.units.register(GUILD, 52, "Officer", "2B-22", "PD")
    inc = await core.create_test_call(GUILD, 1, location="Only St", description="fight")
    _, rest = strip_wake_word("Dispatch, attach me to the call")
    reply = await execute_intent(core, GUILD, officer, parse_rules(rest, CODES), 52)
    assert f"incident {inc['number']}" in reply


@pytest.mark.asyncio
async def test_show_me_available_changes_status(core):
    officer = await core.units.register(GUILD, 53, "Officer", "2B-23", "PD")
    _, rest = strip_wake_word("Dispatch, show me avable")
    reply = await execute_intent(core, GUILD, officer, parse_rules(rest, CODES), 53)
    unit = await core.units.unit(officer["unit_id"])
    assert unit["status_code"] == "10-8" and "10-8" in reply
