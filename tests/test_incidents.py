import time

import pytest

from database.connection import Database
from erlc.models import ServerSnapshot
from tests.conftest import GUILD, settle


def snapshot(calls, players=None):
    return ServerSnapshot.parse({
        "Name": "Test Server", "OwnerId": 1, "CoOwnerIds": [], "CurrentPlayers": 5, "MaxPlayers": 40,
        "JoinKey": "abc", "AccVerifiedReq": "Disabled", "TeamBalance": False,
        "Players": players if players is not None else [
            {"Player": "CallerGuy:555", "Team": "Civilian", "Callsign": None, "Permission": "Normal", "WantedStars": 0,
             "Location": {"LocationX": 1.0, "LocationZ": 2.0, "PostalCode": "218", "StreetName": "Park Street", "BuildingNumber": "12"}},
        ],
        "EmergencyCalls": calls,
    })


def call(number=400, desc="Two car crash, one person hurt", descriptor="Park Street near Postal 218", players=None, started=None):
    return {"Team": "Police", "Caller": 555, "Players": players or [], "Position": [-654.6, 666.5],
            "StartedAt": started or int(time.time()) - 10, "CallNumber": number, "Description": desc,
            "PositionDescriptor": descriptor}


@pytest.mark.asyncio
async def test_new_erlc_call_creates_verified_incident_and_queues_announcement(core):
    res = await core.intake.process_snapshot(GUILD, snapshot([call()]))
    await settle()
    assert len(res.new_incidents) == 1
    inc = await core.incidents.get(res.new_incidents[0])
    assert inc["source"] == "erlc" and inc["verification"] == "verified"
    assert inc["location"] == "Park Street near Postal 218"
    assert inc["description"] == "Two car crash, one person hurt"
    assert inc["caller_display"] == "CallerGuy"
    assert inc["erlc_call_number"] == 400
    assert inc["ai_priority"] in {"medium", "high"}
    assert inc["assessment_source"] == "rules"  # no AI configured in this test
    ann = await core.announcements.next_queued(GUILD)
    assert ann is not None and ann["kind"] == "new_911"
    assert "Park Street near Postal 218" in ann["text"]
    assert str(inc["number"]) in ann["text"]


@pytest.mark.asyncio
async def test_duplicate_calls_are_not_reprocessed(core, db):
    snap = snapshot([call()])
    await core.intake.process_snapshot(GUILD, snap)
    res2 = await core.intake.process_snapshot(GUILD, snap)
    await settle()
    assert res2.new_incidents == []
    assert await db.fetchval("SELECT COUNT(*) FROM incidents") == 1
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE kind = 'new_911'") == 1


@pytest.mark.asyncio
async def test_missing_location_is_never_invented(core):
    res = await core.intake.process_snapshot(GUILD, snapshot([call(descriptor=None)], players=[]))
    await settle()
    inc = await core.incidents.get(res.new_incidents[0])
    assert inc["location"] is None
    ann = await core.announcements.next_queued(GUILD)
    assert "Location unavailable" in ann["text"]


@pytest.mark.asyncio
async def test_invalid_call_without_number_is_counted_not_dropped_silently(core):
    bad = call()
    bad.pop("CallNumber")
    snap = snapshot([bad])
    assert snap.invalid_calls == 1
    res = await core.intake.process_snapshot(GUILD, snap)
    assert res.invalid == 1 and res.new_incidents == []


@pytest.mark.asyncio
async def test_call_update_triggers_escalation(core, db):
    started = int(time.time()) - 5
    await core.intake.process_snapshot(GUILD, snapshot([call(desc="minor fender bender", started=started)]))
    await settle()
    inc = await db.fetchone("SELECT * FROM incidents")
    first_prio = inc["ai_priority"]
    res = await core.intake.process_snapshot(GUILD, snapshot([call(desc="multiple vehicle crash, shots fired, people injured", started=started)]))
    await settle()
    assert res.updated_incidents == [inc["id"]]
    new = await core.incidents.get(inc["id"])
    assert new["ai_priority"] != first_prio
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE kind = 'escalation'") == 1
    hist = [h["event_type"] for h in await core.incidents.history(inc["id"])]
    assert hist.count("assessment") == 2 and "escalation" in hist


@pytest.mark.asyncio
async def test_bootstrap_backlog_is_recorded_but_not_spoken(core, db):
    old = call(started=int(time.time()) - 3600)
    res = await core.intake.process_snapshot(GUILD, snapshot([old]), bootstrap=True)
    await settle()
    assert len(res.new_incidents) == 1
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE kind = 'new_911'") == 0


@pytest.mark.asyncio
async def test_crash_recovery_creates_missing_incident(core, db):
    # Simulate a crash after the call row was stored but before the incident was created.
    await db.execute("INSERT INTO erlc_calls(guild_id, call_number, started_at, description) VALUES (?, 401, ?, 'fire')",
                     (GUILD, int(time.time())))
    row = await db.fetchone("SELECT started_at FROM erlc_calls")
    res = await core.intake.process_snapshot(GUILD, snapshot([call(number=401, desc="house fire", started=row["started_at"])]))
    assert len(res.new_incidents) == 1


@pytest.mark.asyncio
async def test_assignment_flow_and_repeat_cancellation(core, db):
    officer = await core.units.register(GUILD, 42, "J. Smith", "1A-12", "PD")
    unit_id = officer["unit_id"]
    await core.assignments.set_unit_status(unit_id, "10-8", 42)
    inc = await core.create_test_call(GUILD, 1, location="Test Plaza", description="robbery in progress")
    await settle()
    assert inc["verification"] == "test" and inc["source"] == "test"
    inc_after, unit, prev = await core.assignments.attach(inc["id"], unit_id, 42)
    assert inc_after["status"] == "active"
    assert unit["current_incident_id"] == inc["id"]
    assert unit["status_code"] == "EN-ROUTE"
    assigned = await core.incidents.assigned_units(inc["id"])
    assert [u["callsign"] for u in assigned] == ["1A-12"]
    # going 10-8 clears the unit from the incident
    unit = await core.assignments.set_unit_status(unit_id, "10-8", 42)
    assert unit["current_incident_id"] is None
    assert await core.incidents.assigned_units(inc["id"]) == []
    closed = await core.incidents.close(inc["id"], 42, "handled")
    assert closed["status"] == "closed"


@pytest.mark.asyncio
async def test_only_erlc_source_can_be_verified(core):
    from dispatch.incidents import IncidentError
    with pytest.raises(IncidentError):
        await core.incidents.create(GUILD, source="manual", verification="verified", nature="x")
    with pytest.raises(IncidentError):
        await core.incidents.create(GUILD, source="test", verification="unverified", nature="x")


@pytest.mark.asyncio
async def test_priority_approval_workflow(core):
    inc = await core.create_test_call(GUILD, 1, location="Bank", description="shots fired at the bank")
    assert inc["ai_priority"] == "critical"
    assert inc["priority_status"] == "recommended"  # needs supervisor approval (>= high)
    approved = await core.incidents.set_priority(inc["id"], "high", actor_id=99, reason="Shots unconfirmed")
    assert approved["priority"] == "high" and approved["priority_status"] == "overridden"
    assert approved["ai_priority"] == "critical"  # original recommendation preserved
    # a later reassessment must not overwrite the supervisor's decision
    await core.reassess(inc["id"], "test")
    again = await core.incidents.get(inc["id"])
    assert again["priority"] == "high"


@pytest.mark.asyncio
async def test_low_priority_auto_approved(core):
    inc = await core.create_test_call(GUILD, 1, location="Main St", description="noise complaint")
    assert inc["priority_status"] == "approved"


@pytest.mark.asyncio
async def test_repeat_announcements(core, db):
    inc = await core.create_test_call(GUILD, 1, location="Main St", description="car crash with injuries")
    await settle()
    ann = await core.announcements.next_queued(GUILD)
    await core.announcements.mark(ann["id"], "played")
    await db.execute("UPDATE announcements SET played_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (ann["id"],))
    assert await core.repeat_tick(GUILD) == 1
    assert await core.repeat_tick(GUILD) == 0  # already queued -> no duplicate


@pytest.mark.asyncio
async def test_announcement_toggle_disables_type(core, db):
    await core.announcements.set_toggle(GUILD, "new_911", False)
    await core.create_test_call(GUILD, 1, location="Main St", description="fight")
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE kind = 'new_911'") == 0
    toggles = await core.announcements.toggles(GUILD)
    assert toggles["new_911"] is False and all(v for k, v in toggles.items() if k != "new_911")


@pytest.mark.asyncio
async def test_persistence_across_restart(tmp_path):
    from core.events import EventBus
    from dispatch.core import DispatchCore
    from security.audit import AuditLog
    path = tmp_path / "persist.db"
    db1 = Database(path)
    await db1.connect()
    c1 = DispatchCore(db1, EventBus(), AuditLog(db1), None)
    await c1.units.ensure_defaults(GUILD)
    inc = await c1.create_test_call(GUILD, 1, location="Somewhere", description="theft")
    await db1.execute("UPDATE announcements SET status = 'playing'")
    await db1.close()

    db2 = Database(path)
    await db2.connect()
    c2 = DispatchCore(db2, EventBus(), AuditLog(db2), None)
    again = await c2.incidents.get(inc["id"])
    assert again["number"] == inc["number"]
    assert await c2.announcements.recover_interrupted() == 1
    nxt = await c2.incidents.create(GUILD, source="manual", verification="unverified", nature="n")
    assert nxt["number"] == inc["number"] + 1
    await db2.close()


@pytest.mark.asyncio
async def test_close_all_closes_every_incident_and_cancels_repeats(core, db):
    snap = snapshot([call(400), call(401, desc="Shots fired at the bank")])
    await core.intake.process_snapshot(GUILD, snap)
    await settle()
    assert len(await core.incidents.list_active(GUILD)) == 2
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE status = 'queued'") > 0
    closed = await core.incidents.close_all(GUILD, 1, "End of session")
    await settle()
    assert len(closed) == 2
    assert await core.incidents.list_active(GUILD) == []
    assert await db.fetchval("SELECT COUNT(*) FROM announcements WHERE guild_id = ? AND status = 'queued' "
                             "AND kind IN ('repeat','new_911','escalation')", (GUILD,)) == 0
    assert await core.repeat_tick(GUILD) == 0
    # Calls still listed in-game must not re-open as new incidents.
    res = await core.intake.process_snapshot(GUILD, snap)
    assert res.new_incidents == []
    assert await core.incidents.close_all(GUILD, 1) == []
