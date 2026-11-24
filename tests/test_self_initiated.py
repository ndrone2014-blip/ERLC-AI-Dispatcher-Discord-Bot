"""Officer-initiated activity by voice: traffic stops, subject stops, pursuits."""
import pytest

from ai.conversation import parse_rules, strip_wake_word
from database.models import DEFAULT_STATUS_CODES
from tests.conftest import GUILD, settle
from voice.commands import execute_intent

CODES = [{"code": c, "label": l, "availability": a, "sort": s} for c, l, a, s in DEFAULT_STATUS_CODES]


def parse(heard):
    woke, rest = strip_wake_word(heard)
    assert woke
    return parse_rules(rest, CODES)


@pytest.mark.parametrize("heard,kind,location,plate", [
    ("Dispatch, show me on a traffic stop", "traffic_stop", None, None),
    ("Dispatch, show me for a traffic stop", "traffic_stop", None, None),
    ("Dispatch, show me on a traffic stop at Main Street and Fifth.", "traffic_stop", "Main Street And Fifth", None),
    ("Dispatch I'm on a traffic stop on Park Avenue, plate ABC 123", "traffic_stop", "Park Avenue", "ABC123"),
    ("Dispatch, show me ten eleven near the gas station", "traffic_stop", "The Gas Station", None),
    ("This patch, traffic stop at postal 218", "traffic_stop", "Postal 218", None),
    ("Dispatch, show me on a subject stop at the bank", "subject_stop", "The Bank", None),
    ("Dispatch, I'm in pursuit heading north on the highway", "pursuit", "The Highway", None),
    ("Dispatch, I'm in pursuit", "pursuit", None, None),
    ("Dispatch, show me in a foot pursuit near the park", "foot_pursuit", "The Park", None),
])
def test_parse_self_initiated(heard, kind, location, plate):
    got = parse(heard)
    assert got is not None and got.name == "self_initiated", (heard, got)
    assert got.args["kind"] == kind
    assert got.args["location"] == location, (heard, got.args)
    assert got.args["plate"] == plate


@pytest.mark.parametrize("heard,plate", [
    ("Dispatch, traffic stop at Main Street, plate Adam Boy Charles one two three", "ABC123"),
    ("Dispatch, traffic stop at Main Street, plate alpha bravo 4 5 6", "AB456"),
    ("Dispatch, traffic stop, plate ABC-123", "ABC123"),
    ("Dispatch, traffic stop, plate number is XYZ 789 on Park Road", "XYZ789"),
    ("Dispatch, traffic stop at Main Street, Play2BC 123.", "2BC123"),   # Whisper mishearing of "plate"
])
def test_plates(heard, plate):
    assert parse(heard).args["plate"] == plate


def test_backup_request_is_not_a_new_pursuit():
    assert parse("Dispatch, request backup for the pursuit").name == "backup"


def test_clear_from_stop_is_detach():
    assert parse("Dispatch, I'm clear from the traffic stop").name == "detach"
    assert parse("Dispatch, code 4, clear the stop").name == "detach"


@pytest.mark.asyncio
async def test_traffic_stop_creates_incident_and_status(core, db):
    o = await core.units.register(GUILD, 60, "Officer", "7 ADAM 14", "PD")
    reply = await execute_intent(core, GUILD, o, parse("Dispatch, show me on a traffic stop at Main Street, plate XYZ 789"), 60)
    await settle()
    unit = await core.units.unit(o["unit_id"])
    inc = await core.incidents.get(unit["current_incident_id"])
    assert unit["status_code"] == "10-11"
    assert inc["nature"] == "Traffic stop — XYZ789" and inc["location"] == "Main Street"
    assert inc["verification"] == "unverified" and inc["caller_display"] == "Officer-initiated: 7 ADAM 14"
    assert f"Incident {inc['number']}" in reply and "Main Street" in reply and "X Y Z 7 8 9" in reply
    # no "new 911 call" broadcast and no duplicate "attached" announcement for a self-initiated stop
    kinds = [r["kind"] for r in await db.fetchall("SELECT kind FROM announcements WHERE incident_id = ?", (inc["id"],))]
    assert "new_911" not in kinds and "unit_assignment" not in kinds


@pytest.mark.asyncio
async def test_traffic_stop_without_location_asks(core):
    o = await core.units.register(GUILD, 61, "Officer", "2B-1", "PD")
    reply = await execute_intent(core, GUILD, o, parse("Dispatch, show me on a traffic stop"), 61)
    assert "Advise your location" in reply


@pytest.mark.asyncio
async def test_clearing_stop_closes_it(core):
    o = await core.units.register(GUILD, 62, "Officer", "2B-2", "PD")
    await execute_intent(core, GUILD, o, parse("Dispatch, traffic stop at Elm Road"), 62)
    o = await core.units.officer_by_discord(GUILD, 62)
    inc_id = o["current_incident_id"]
    reply = await execute_intent(core, GUILD, o, parse("Dispatch, I'm clear from the traffic stop"), 62)
    assert "closed" in reply
    assert (await core.incidents.get(inc_id))["status"] == "closed"


@pytest.mark.asyncio
async def test_pursuit_requests_backup(core, db):
    o = await core.units.register(GUILD, 63, "Officer", "2B-3", "PD")
    reply = await execute_intent(core, GUILD, o, parse("Dispatch, I'm in pursuit on Route 1"), 63)
    await settle()
    assert "backup requested" in reply
    assert await db.fetchval("SELECT COUNT(*) FROM backup_requests WHERE status = 'open'") == 1
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE kind = 'backup_request'") == 1


@pytest.mark.asyncio
async def test_dispatcher_assignment_still_announced(core, db):
    o = await core.units.register(GUILD, 64, "Officer", "2B-4", "PD")
    inc = await core.create_test_call(GUILD, 1, location="Main", description="theft")
    await core.assignments.attach(inc["id"], o["unit_id"], actor_id=999)  # a dispatcher, not the officer
    await settle()
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE kind = 'unit_assignment'") == 1


@pytest.mark.asyncio
async def test_existing_guild_gets_traffic_stop_code(core, db):
    await db.execute("DELETE FROM status_codes WHERE code = '10-11'")
    await db.set_config(GUILD, "status_defaults_version", 1)
    await core.units.ensure_defaults(GUILD)
    assert await core.units.status_code(GUILD, "10-11")
    await core.units.remove_status_code(GUILD, "10-11", 1)
    await core.units.ensure_defaults(GUILD)  # removed by admin -> stays removed
    assert await core.units.status_code(GUILD, "10-11") is None
