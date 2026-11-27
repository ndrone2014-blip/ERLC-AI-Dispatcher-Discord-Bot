"""v4.0 Command Center: community platform services (no Discord, no network)."""
import pytest
import pytest_asyncio

from community.actions import RecordingActions
from community.assistant import recognise
from community.common import ServiceError, parse_duration, safe_format
from community.embeds import validate_embed
from community.platform import CommunityPlatform
from community.roblox import RobloxUser
from community.verification import code_in_text
from core.events import EventBus
from security.audit import AuditLog
from security.permissions import DEFAULT_PERMISSIONS, Actor, PermissionManager
from security.validation import ValidationError
from tests.conftest import GUILD, settle

OTHER_GUILD = 2222


class FakeRoblox:
    oauth_configured = False

    def __init__(self):
        self.users = {"BuilderBob": RobloxUser(id=42, name="BuilderBob", display_name="Bob", description="", created="2015-01-01T00:00:00Z"),
                      "NewKid": RobloxUser(id=77, name="NewKid", display_name="Kid", description="", created="2099-01-01T00:00:00Z")}

    async def user_by_username(self, name):
        return self.users.get(name)

    async def user(self, uid):
        return next((u for u in self.users.values() if u.id == uid), None)


@pytest_asyncio.fixture
async def platform(db, core):
    actions = RecordingActions(members={10: {"name": "alice", "display_name": "Alice"}, 20: {"name": "bob"}, 30: {"name": "carl"}})
    p = CommunityPlatform(db, core.bus, AuditLog(db), PermissionManager(db), actions=actions, roblox=FakeRoblox(), dispatch=core)
    yield p
    await settle()


ALL = set(DEFAULT_PERMISSIONS)


# ------------------------------------------------------------------ helpers
def test_duration_parsing_and_safe_format():
    assert parse_duration("10m") == 600
    assert parse_duration("2h 30m") == 9000
    assert parse_duration("90") == 5400
    with pytest.raises(ValidationError):
        parse_duration("soon")
    assert safe_format("Hi {user} {unknown} {__class__}", {"user": "<@1>"}) == "Hi <@1> {unknown} {__class__}"
    assert "@​everyone" in safe_format("@everyone", {})


def test_embed_validation_limits():
    ok = validate_embed({"title": "T", "color": "#3CC8F0", "fields": [{"name": "a", "value": "b"}],
                         "buttons": [{"label": "Rules", "url": "https://example.com/rules"}]})
    assert ok["color"] == 0x3CC8F0 and ok["buttons"][0]["label"] == "Rules"
    with pytest.raises(ValidationError):
        validate_embed({})
    with pytest.raises(ValidationError):
        validate_embed({"title": "x", "image": "javascript:alert(1)"})
    with pytest.raises(ValidationError):
        validate_embed({"title": "x", "fields": [{"name": "n", "value": "v"}] * 26})


def test_verification_code_matching_ignores_formatting():
    assert code_in_text("pulse amber falcon river", "Hi!\nPULSE  amber, falcon - river :)")
    assert not code_in_text("pulse amber falcon river", "pulse amber river falcon")


@pytest.mark.parametrize("text", ["Can you show active calls?", "What calls are currently open?", "Display the active incidents.",
                                  "Are there any incidents right now?", "Show me the current dispatch list."])
def test_assistant_recognises_active_call_phrasings(text):
    it = recognise(text)
    assert it is not None and it.name == "active_incidents"


@pytest.mark.parametrize("text,intent", [("which units are available", "units_available"), ("show incident 104", "incident_detail"),
                                         ("close incident #12", "close_incident"), ("start my shift", "shift_start"),
                                         ("clock out", "shift_end"), ("end my shift", "shift_end"), ("who is on shift", "staff_on_shift"),
                                         ("am i verified", "my_verification"), ("any sessions today?", "sessions"),
                                         ("am i on duty", "my_shift")])
def test_assistant_other_intents(text, intent):
    it = recognise(text)
    assert (it.name if it else None) == intent


# ------------------------------------------------------------------ permissions
@pytest.mark.asyncio
async def test_new_permission_groups(db):
    pm = PermissionManager(db)
    await pm.set_group_roles(GUILD, "moderator", [500])
    mod = Actor(user_id=1, role_ids={500})
    assert await pm.has(GUILD, mod, "mod.warn")
    assert not await pm.has(GUILD, mod, "staff.manage")
    perms = await pm.all_for(GUILD, mod)
    assert "dashboard.view" in perms and "automation.manage" not in perms
    # roles mapped in one server mean nothing in another
    assert not await pm.has(OTHER_GUILD, mod, "mod.warn")


# ------------------------------------------------------------------ verification
@pytest.mark.asyncio
async def test_profile_code_verification_end_to_end(platform):
    p, actions = platform, platform.actions
    await p.verification.update_settings(GUILD, {"verified_role_id": 900, "unverified_role_id": 901,
                                                 "nickname_format": "{display} (@{roblox})"}, actor_id=1)
    start = await p.verification.start_profile(GUILD, 10, "BuilderBob")
    assert start["code"].startswith("pulse ")
    # code not in the profile yet -> honest failure, nothing linked
    with pytest.raises(ServiceError):
        await p.verification.check_profile(GUILD, 10)
    assert await p.verification.get_link(GUILD, 10) is None
    p.verification.roblox.users["BuilderBob"].description = f"I love ER:LC. {start['code'].upper()}"
    result = await p.verification.check_profile(GUILD, 10)
    assert result["roblox"]["name"] == "BuilderBob" and not result["problems"]
    link = await p.verification.get_link(GUILD, 10)
    assert link["roblox_id"] == "42" and link["method"] == "profile_code"
    assert ("add_role", (GUILD, 10, 900), {}) in actions.calls
    assert ("remove_role", (GUILD, 10, 901), {}) in actions.calls
    assert actions.called("set_nickname")[0][1][2] == "Bob (@BuilderBob)"
    # another member cannot claim the same Roblox account
    with pytest.raises(ServiceError):
        await p.verification.start_profile(GUILD, 20, "BuilderBob")
    # but the same account can be verified in a different server
    await p.verification.start_profile(OTHER_GUILD, 20, "BuilderBob")
    hist = await p.verification.history(GUILD)
    assert hist["items"][0]["action"] == "verified"
    assert await p.verification.unlink(GUILD, 10, actor_id=1, reason="test")
    assert await p.verification.get_link(GUILD, 10) is None


@pytest.mark.asyncio
async def test_verification_requirements(platform):
    p = platform
    await p.verification.update_settings(GUILD, {"min_account_age_days": 30}, actor_id=1)
    with pytest.raises(ServiceError, match="day"):
        await p.verification.start_profile(GUILD, 10, "NewKid")
    with pytest.raises(ServiceError, match="No Roblox account"):
        await p.verification.start_profile(GUILD, 10, "Nobody123")
    with pytest.raises(ValidationError):
        await p.verification.start_profile(GUILD, 10, "bad name!")
    with pytest.raises(ServiceError, match="not set up"):
        await p.verification.start_oauth(GUILD, 10)


# ------------------------------------------------------------------ staff & shifts
@pytest.mark.asyncio
async def test_staff_lifecycle_and_rank_roles(platform):
    p = platform
    await p.staff.update_settings(GUILD, {"ranks": [{"name": "Trial", "role_id": 1}, {"name": "Moderator", "role_id": 2},
                                                    {"name": "Senior", "role_id": 3}]}, actor_id=1)
    await p.staff.appoint(GUILD, 10, "Trial", actor_id=1)
    with pytest.raises(ServiceError):
        await p.staff.appoint(GUILD, 10, "Trial", actor_id=1)
    r = await p.staff.change_rank(GUILD, 10, 1, direction="up")
    assert r["staff"]["rank"] == "Moderator" and r["action"] == "promoted"
    assert ("remove_role", (GUILD, 10, 1), {}) in p.actions.calls and ("add_role", (GUILD, 10, 2), {}) in p.actions.calls
    r = await p.staff.change_rank(GUILD, 10, 1, direction="down")
    assert r["action"] == "demoted"
    with pytest.raises(ServiceError):
        await p.staff.change_rank(GUILD, 10, 1, direction="down")
    await p.staff.add_note(GUILD, 10, 1, "Great first week")
    prof = await p.staff.profile(GUILD, 10, include_notes=True)
    assert prof["notes"][0]["note"] == "Great first week" and len(prof["history"]) == 3
    await p.staff.remove(GUILD, 10, 1, "inactive")
    assert (await p.staff.get(GUILD, 10))["status"] == "removed"
    assert (await p.staff.list(GUILD))["total"] == 0


@pytest.mark.asyncio
async def test_shifts_one_at_a_time(platform):
    p = platform
    s = await p.shifts.start(GUILD, 10, "Patrol")
    assert s["shift_type"] == "Patrol"
    with pytest.raises(ServiceError):
        await p.shifts.start(GUILD, 10, "Patrol")
    with pytest.raises(ValidationError):
        await p.shifts.start(GUILD, 20, "Underwater Basket Weaving")
    assert len(await p.shifts.active(GUILD)) == 1
    ended = await p.shifts.end(GUILD, 10, 10, notes="quiet")
    assert ended["duration_s"] is not None and ended["notes"] == "quiet"
    assert (await p.shifts.summary(GUILD, 10))["count"] == 1
    with pytest.raises(ServiceError):
        await p.shifts.end(GUILD, 10, 10)


@pytest.mark.asyncio
async def test_leave_request_flow(platform):
    p = platform
    await p.staff.appoint(GUILD, 20, "Staff", actor_id=1)
    import datetime as dt
    today = dt.date.today()
    req = await p.staff.request_leave(GUILD, 20, today.isoformat(), (today + dt.timedelta(days=3)).isoformat(), "Holiday")
    with pytest.raises(ServiceError):
        await p.staff.request_leave(GUILD, 20, today.isoformat(), today.isoformat(), "again")
    await p.staff.review_leave(GUILD, req["id"], True, actor_id=1)
    assert (await p.staff.get(GUILD, 20))["status"] == "leave"


# ------------------------------------------------------------------ applications
@pytest.mark.asyncio
async def test_application_form_submit_and_review(platform):
    p = platform
    form = await p.applications.create_from_preset(GUILD, "Dispatch", actor_id=1)
    assert form["enabled"] == 0  # presets start closed so nothing goes live by accident
    form = await p.applications.save_form(GUILD, {**form, "enabled": True, "approve_role_id": 777}, actor_id=1, form_id=form["id"])
    answers = {q["id"]: ("yes" if q["type"] in {"yesno", "confirm"} else "Because I like radios") for q in form["questions"]}
    with pytest.raises(ValidationError):
        await p.applications.submit(GUILD, form["id"], 10, {**answers, form["questions"][-1]["id"]: False}, set())
    app = await p.applications.submit(GUILD, form["id"], 10, answers, set())
    assert app["status"] == "submitted" and len(app["answers"]) == len(form["questions"])
    with pytest.raises(ServiceError, match="open application"):
        await p.applications.submit(GUILD, form["id"], 10, answers, set())
    with pytest.raises(ValidationError):
        await p.applications.set_status(GUILD, app["number"], "awaiting_response", 1)  # needs a question for the applicant
    await p.applications.set_status(GUILD, app["number"], "awaiting_response", 1, "Do you have a mic?")
    await p.applications.respond(GUILD, app["number"], 10, "Yes I do")
    done = await p.applications.set_status(GUILD, app["number"], "approved", 1, "Welcome!")
    assert done["status"] == "approved" and ("add_role", (GUILD, 10, 777), {}) in p.actions.calls
    notes = await p.notifications.list_for(GUILD, 1, ALL)
    assert any(n["kind"] == "application" for n in notes)
    # notifications are scoped by permission
    assert not await p.notifications.list_for(GUILD, 99, {"dashboard.view"})


def test_question_validation():
    from community.applications import validate_questions
    with pytest.raises(ValidationError):
        validate_questions([{"label": "Pick", "type": "choice", "options": ["only one"]}])
    with pytest.raises(ValidationError):
        validate_questions([])
    qs = validate_questions([{"label": "Agree?", "type": "confirm", "required": False}])
    assert qs[0]["required"] is True


# ------------------------------------------------------------------ moderation
@pytest.mark.asyncio
async def test_moderation_cases_and_escalation(platform):
    p = platform
    await p.moderation.update_settings(GUILD, {"warn_threshold": 2, "warn_threshold_action": "timeout",
                                               "warn_threshold_duration": "30m"}, actor_id=1)
    c1 = await p.moderation.create(GUILD, "warn", 20, 10, reason="Spamming")
    assert c1["number"] == 1 and c1["status"] == "active" and "DM delivered" in c1["outcome"]
    c2 = await p.moderation.create(GUILD, "warn", 20, 10, reason="Spamming again")
    assert c2["escalation"]["action"] == "timeout"
    assert p.actions.called("timeout")[0][1][2] == 1800
    with pytest.raises(ServiceError):
        await p.moderation.create(GUILD, "warn", 10, 10, reason="me")
    hist = await p.moderation.history(GUILD, 20)
    assert [c["action"] for c in hist] == ["timeout", "warn", "warn"]
    await p.moderation.revoke(GUILD, 1, 10, "mistake")
    assert (await p.moderation.get(GUILD, 1))["status"] == "revoked"
    assert (await p.moderation.list(GUILD, action="warn"))["total"] == 2


@pytest.mark.asyncio
async def test_failed_discord_action_is_recorded_honestly(platform):
    p = platform
    p.actions.fail["ban"] = "Missing Permissions"
    with pytest.raises(ServiceError, match="Missing Permissions"):
        await p.moderation.create(GUILD, "ban", 30, 10, reason="raiding")
    case = await p.moderation.get(GUILD, 1)
    assert case["status"] == "failed" and "Missing Permissions" in case["outcome"]


@pytest.mark.asyncio
async def test_hierarchy_is_enforced(platform):
    p = platform
    p.actions.hierarchy_errors[30] = "Their highest role is above yours."
    with pytest.raises(ServiceError, match="above yours"):
        await p.moderation.create(GUILD, "kick", 30, 10, reason="x")
    assert not p.actions.called("kick")


# ------------------------------------------------------------------ automation
@pytest.mark.asyncio
async def test_automation_runs_once_per_event_and_respects_conditions(platform):
    p = platform
    await p.automation.save(GUILD, {"name": "Ban alert", "trigger": "mod.case",
                                    "conditions": [{"field": "action", "op": "eq", "value": "ban"}],
                                    "actions": [{"type": "send_message", "channel_id": 555, "content": "Ban case #{case_number} for {user}"}]},
                            actor_id=1)
    await p.moderation.create(GUILD, "warn", 20, 10, reason="x")
    await settle()
    assert not [c for c in p.actions.called("send") if c[1][1] == 555]
    await p.moderation.create(GUILD, "ban", 20, 10, reason="raid")
    await settle()
    sends = [c for c in p.actions.called("send") if c[1][1] == 555]
    assert len(sends) == 1 and sends[0][2]["content"] == "Ban case #2 for <@20>"
    # replaying the same event does not run the automation again
    await p.bus.publish("mod.case", GUILD, case_number=2, action="ban", user_id=20)
    await settle()
    assert len([c for c in p.actions.called("send") if c[1][1] == 555]) == 1
    runs = await p.automation.runs(GUILD)
    assert len(runs) == 1 and runs[0]["ok"] == 1


@pytest.mark.asyncio
async def test_automation_failure_is_visible(platform):
    p = platform
    p.actions.fail["add_role"] = "Missing Permissions"
    await p.automation.save(GUILD, {"name": "Role on verify", "trigger": "verification.completed",
                                    "actions": [{"type": "add_role", "role_id": 4}]}, actor_id=1)
    await p.bus.publish("verification.completed", GUILD, user_id=10, roblox="x", event_key="v1")
    await settle()
    a = (await p.automation.list(GUILD))[0]
    assert "Missing Permissions" in a["last_error"]
    assert any(n["kind"] == "automation" for n in await p.notifications.list_for(GUILD, 1, ALL))


def test_automation_validation():
    from community.automation import validate
    with pytest.raises(ValidationError):
        validate({"name": "x", "trigger": "nope", "actions": []})
    with pytest.raises(ValidationError):
        validate({"name": "x", "trigger": "member.joined", "actions": [{"type": "send_message", "content": "hi"}]})
    with pytest.raises(ValidationError):
        validate({"name": "x", "trigger": "member.joined", "conditions": [{"field": "case_number", "value": 1}],
                  "actions": [{"type": "log", "content": "hi"}]})


# ------------------------------------------------------------------ sessions, members, embeds, analytics
@pytest.mark.asyncio
async def test_sessions_and_reminders(platform):
    p = platform
    import datetime as dt
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    await p.sessions.update_settings(GUILD, {"announce_channel_id": 321}, actor_id=1)
    s = await p.sessions.create(GUILD, 1, name="Friday Patrol", scheduled_for=soon, reminder_minutes=15)
    assert s["number"] == 1 and s["message_id"]
    await p.sessions.join(GUILD, 1, 20)
    assert await p.sessions.tick(GUILD) == 1
    assert await p.sessions.tick(GUILD) == 0  # only once
    await p.sessions.start(GUILD, 1, 1)
    ended = await p.sessions.end(GUILD, 1, 1)
    assert ended["status"] == "ended" and ended["participant_count"] == 1


@pytest.mark.asyncio
async def test_member_directory_and_isolation(platform):
    p = platform
    await p.members.bulk_sync(GUILD, [{"id": 10, "name": "alice"}, {"id": 20, "name": "bob"}, {"id": 99, "name": "botty", "bot": True}])
    await p.members.bulk_sync(OTHER_GUILD, [{"id": 30, "name": "carl"}])
    d = await p.members.directory(GUILD)
    assert {m["username"] for m in d["items"]} == {"alice", "bob"}
    assert (await p.members.directory(GUILD, q="car"))["total"] == 0
    await p.members.on_leave(GUILD, 20, "bob")
    assert (await p.members.counts(GUILD))["total"] == 1
    assert (await p.members.directory(GUILD, filter="left"))["total"] == 1


@pytest.mark.asyncio
async def test_welcome_message_uses_template(platform):
    p = platform
    t = await p.embeds.create_from_preset(GUILD, "Welcome", actor_id=1)
    await p.update_settings(GUILD, "welcome_settings", __import__("community.members", fromlist=["x"]).WELCOME_DEFAULTS,
                            {"enabled": True, "channel_id": 444, "template_id": t["id"], "auto_role_ids": [12]})
    problems = await p.members.on_join(GUILD, {"id": 10, "name": "alice"})
    assert problems == []
    send = [c for c in p.actions.called("send") if c[1][1] == 444][0]
    assert "<@10>" in send[2]["embed"]["description"]
    assert ("add_role", (GUILD, 10, 12), {}) in p.actions.calls


@pytest.mark.asyncio
async def test_analytics_series_are_real(platform):
    p = platform
    await p.members.on_join(GUILD, {"id": 10, "name": "alice"})
    await p.moderation.create(GUILD, "warn", 20, 10, reason="x")
    data = await p.analytics.series(GUILD, ["member_joins", "moderation", "applications"], "7d")
    assert len(data["days"]) == 7
    assert data["metrics"]["member_joins"]["total"] == 1
    assert data["metrics"]["moderation"]["total"] == 1
    assert data["metrics"]["applications"]["total"] == 0
    csv = await p.analytics.export_csv(GUILD, ["member_joins"], "7d")
    assert csv.splitlines()[0] == "date,Member joins"


@pytest.mark.asyncio
async def test_assistant_answers_from_real_data(platform, core):
    p = platform
    r = await p.assistant.ask(GUILD, 10, ALL, "are there any active calls?")
    assert r.source == "local" and "no active incidents" in r.text
    await core.create_manual_incident(GUILD, 10, nature="Structure fire", location="Bank")
    await settle()
    r = await p.assistant.ask(GUILD, 10, ALL, "show me the current dispatch list")
    assert "1 active" in r.text and "Structure fire" in r.lines[0]
    denied = await p.assistant.ask(GUILD, 10, {"assistant.use"}, "show active calls")
    assert not denied.ok
    r = await p.assistant.ask(GUILD, 10, ALL, "start my shift")
    assert r.confirm and r.confirm["action"] == "shift_start"
    assert await p.shifts.current(GUILD, 10) is None  # nothing happens without confirmation
    done = await p.assistant.confirm(GUILD, 10, ALL, "shift_start", r.confirm["args"])
    assert done.ok and await p.shifts.current(GUILD, 10)
    none = await p.assistant.ask(GUILD, 10, ALL, "what's the meaning of life", allow_ai=False)
    assert none.source == "none"
