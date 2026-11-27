"""v5.0 Platform Overhaul: ER:LC live, in-game moderation, BOLOs, staff overview, activity feed, search."""
import pytest
import pytest_asyncio

from community.actions import RecordingActions
from community.platform import CommunityPlatform
from core import activity
from erlc.live import ErlcLive, split_player
from erlc.models import ServerSnapshot
from security.audit import AuditLog
from security.permissions import DEFAULT_PERMISSIONS, PermissionManager
from tests.conftest import GUILD, settle
from tests.test_dashboard_api import FakeBot, ctx
from web.api import Api, ApiError

ALL = set(DEFAULT_PERMISSIONS)


def snap(players, **extra):
    data = {"Name": "Test RP", "CurrentPlayers": len(players), "MaxPlayers": 40,
            "Players": [{"Player": f"{n}:{i}", "Team": t, "Permission": perm} for n, i, t, perm in players], **extra}
    return ServerSnapshot.parse(data)


class FakeErlc:
    """Stands in for the bot's ER:LC bridge: records commands instead of calling the API."""

    def __init__(self, live=None, fail=None):
        self.commands, self.live, self.fail = [], live, fail

    async def run_command(self, guild_id, command):
        if self.fail:
            raise RuntimeError(self.fail)
        self.commands.append(command)
        return {}


@pytest_asyncio.fixture
async def env(db, core):
    p = CommunityPlatform(db, core.bus, AuditLog(db), PermissionManager(db), actions=RecordingActions(), dispatch=core)
    live = ErlcLive(db, core.bus, notify=p.notifications.create)
    erlc = FakeErlc(live)
    p.erlc_moderation.erlc = erlc
    bot = FakeBot(db, core, p)
    bot.erlc_live = live
    yield {"api": Api(bot), "p": p, "live": live, "erlc": erlc, "db": db, "core": core}
    await settle()


# ---------------------------------------------------------------------------- activity
@pytest.mark.asyncio
async def test_activity_is_recorded_even_when_discord_logging_is_off(env):
    p, api = env["p"], env["api"]
    await p.log(GUILD, "moderation", "Warning issued", fields=[("Member", "<@5>", True)], actor_id=10)
    await p.log(GUILD, "shifts", "Shift Started", actor_id=10)
    await p.log(GUILD, "radio", "Radio chatter")  # skipped: too chatty for the feed
    await p.log(2222, "shifts", "Other server")
    feed = await api.dispatch("GET", "/activity", ctx())
    assert [i["title"] for i in feed["items"]] == ["Shift Started", "Warning issued"]
    mod = await api.dispatch("GET", "/activity", ctx(query={"group": "moderation"}))
    assert mod["total"] == 1 and mod["items"][0]["link"] == "moderation/discord"
    found = await api.dispatch("GET", "/activity", ctx(query={"q": "shift"}))
    assert found["total"] == 1
    assert {g["key"] for g in feed["groups"]} >= {"players", "moderation", "bolos"}


# ---------------------------------------------------------------------------- ER:LC live
def test_split_player():
    assert split_player("Builder_Bob:12345") == ("Builder_Bob", 12345)
    assert split_player("weird") == ("weird", None)


@pytest.mark.asyncio
async def test_first_poll_is_a_baseline_then_joins_and_leaves_are_detected(env):
    live, db = env["live"], env["db"]
    first = await live.process(GUILD, snap([("Alice", 1, "Police", "Normal"), ("Mod", 2, "Civilian", "Server Moderator")]))
    assert first["baseline"] and not first["joins"]
    assert await db.fetchval("SELECT COUNT(*) FROM erlc_player_events") == 0
    second = await live.process(GUILD, snap([("Mod", 2, "Civilian", "Server Moderator"), ("Bob", 3, "Fire", "Normal")]))
    assert [p["name"] for p in second["joins"]] == ["Bob"] and [p["name"] for p in second["leaves"]] == ["Alice"]
    row = await db.fetchone("SELECT * FROM erlc_players WHERE guild_id = ? AND roblox_id = 1", (GUILD,))
    assert row["online"] == 0 and row["last_leave"]
    s = live.snapshot(GUILD)
    assert s["current_players"] == 2 and [p["name"] for p in s["staff_online"]] == ["Mod"]
    assert s["sections"]["join_logs"] is False  # not returned by the API = unavailable, not empty
    feed = await env["api"].dispatch("GET", "/activity", ctx(query={"group": "players"}))
    assert {i["title"] for i in feed["items"]} == {"Bob joined the server", "Alice left the server"}
    off = await live.server_offline(GUILD)
    assert {p["name"] for p in off["leaves"]} == {"Mod", "Bob"}


@pytest.mark.asyncio
async def test_extra_sections_are_parsed_and_can_be_disabled(env):
    live = env["live"]
    await live.process(GUILD, snap([("Alice", 1, "Police", "Normal")],
                                   JoinLogs=[{"Join": True, "Timestamp": 1700000000, "Player": "Alice:1"}],
                                   CommandLogs=[{"Player": "Mod:2", "Timestamp": 1700000001, "Command": ":h hi"}],
                                   Staff={"Admins": {"2": "Mod"}, "CoOwners": [9]}, Queue=[5, 6]))
    assert live.section(GUILD, "join_logs")[0]["player"] == "Alice"
    assert live.section(GUILD, "command_logs")[0]["command"] == ":h hi"
    assert {s["group"] for s in live.section(GUILD, "staff")} == {"Admins", "CoOwners"}
    assert live.section(GUILD, "queue") == ["5", "6"]
    assert await live.extra_flags(GUILD)
    live.extras_failed(GUILD, "Resource restricted")
    assert await live.extra_flags(GUILD) == {}


@pytest.mark.asyncio
async def test_player_with_a_bolo_joining_notifies_staff_but_never_bans(env):
    api, live, p = env["api"], env["live"], env["p"]
    await api.dispatch("POST", "/bolos", ctx(body={"player": "Suspect_1", "reason": "Reported for RDM", "priority": "high"}))
    await live.process(GUILD, snap([]))
    await live.process(GUILD, snap([("Suspect_1", 77, "Civilian", "Normal")]))
    notes = await p.notifications.list_for(GUILD, 10, ALL)
    assert any("BOLO #1 player in-game" in n["title"] for n in notes)
    assert env["erlc"].commands == []  # nothing was run in-game
    assert await env["db"].fetchval("SELECT COUNT(*) FROM erlc_mod_records") == 0
    data = await api.dispatch("GET", "/erlc/live", ctx())
    assert data["live"]["players"][0]["bolo"]["number"] == 1


# ---------------------------------------------------------------------------- in-game moderation
@pytest.mark.asyncio
async def test_in_game_records_and_commands(env):
    api, erlc = env["api"], env["erlc"]
    w = await api.dispatch("POST", "/moderation/records", ctx(body={"action": "warning", "player": "Rule_Breaker", "reason": "Fail RP"}))
    assert w["number"] == 1 and w["in_game"] == "not_requested" and erlc.commands == []
    k = await api.dispatch("POST", "/moderation/records", ctx(body={"action": "kick", "player": "Rule_Breaker", "reason": "RDM\n:ban everyone",
                                                                    "execute": True, "confirm": True}))
    assert k["in_game"] == "sent" and erlc.commands == [":kick Rule_Breaker RDM :ban everyone"]  # one line, no injected command
    b = await api.dispatch("POST", "/moderation/records", ctx(body={"action": "ban", "player": "Rule_Breaker", "reason": "Repeat", "execute": True,
                                                                    "confirm": True}))
    assert b["status"] == "active" and erlc.commands[-1] == ":ban Rule_Breaker"
    await api.dispatch("POST", "/moderation/records", ctx(body={"action": "unban", "player": "Rule_Breaker", "reason": "Appeal", "execute": True}))
    assert (await api.dispatch("GET", "/moderation/records/3", ctx()))["status"] == "revoked"
    prof = await api.dispatch("GET", "/moderation/player/Rule_Breaker", ctx())
    assert prof["counts"] == {"warning": 1, "kick": 1, "ban": 1, "unban": 1} and not prof["active_ban"]
    stats = await api.dispatch("GET", "/moderation/stats", ctx())
    assert stats["in_game"]["kick"] == 1 and stats["moderators"][0]["total"] == 4


@pytest.mark.asyncio
async def test_in_game_moderation_permissions_and_validation(env):
    api = env["api"]
    mod_only = {"dashboard.view", "erlc.view", "erlc.moderate"}
    with pytest.raises(ApiError) as e:
        await api.dispatch("POST", "/moderation/records", ctx(mod_only, body={"action": "ban", "player": "X_y", "reason": "r", "confirm": True}))
    assert e.value.status == 403
    with pytest.raises(ApiError, match="Confirm"):
        await api.dispatch("POST", "/moderation/records", ctx(body={"action": "kick", "player": "X_y", "reason": "r"}))
    with pytest.raises(ApiError) as e:
        await api.dispatch("POST", "/moderation/records", ctx(body={"action": "warning", "player": "bad name; :ban all", "reason": "r"}))
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        await api.dispatch("GET", "/moderation/records", ctx({"dashboard.view"}))
    assert e.value.status == 403
    with pytest.raises(ApiError, match="no active ban"):
        await api.dispatch("POST", "/moderation/records", ctx(body={"action": "unban", "player": "Nobody_1", "reason": "r"}))


@pytest.mark.asyncio
async def test_failed_in_game_command_is_recorded_honestly(env):
    env["p"].erlc_moderation.erlc = FakeErlc(fail="Private server is offline")
    rec = await env["api"].dispatch("POST", "/moderation/records", ctx(body={"action": "kick", "player": "Some_One", "reason": "x",
                                                                              "execute": True, "confirm": True}))
    assert rec["in_game"] == "failed" and "offline" in rec["in_game_detail"] and "warning" in rec


@pytest.mark.asyncio
async def test_tempbans_expire_and_are_lifted_in_game(env):
    api, db, erlc = env["api"], env["db"], env["erlc"]
    await api.dispatch("POST", "/moderation/records", ctx(body={"action": "tempban", "player": "Temp_Guy", "reason": "x", "duration": "2h",
                                                                "execute": True, "confirm": True}))
    await db.execute("UPDATE erlc_mod_records SET expires_at = '2000-01-01T00:00:00.000000Z'")
    assert await env["p"].erlc_moderation.expire_tempbans(GUILD) == 1
    assert erlc.commands[-1] == ":unban Temp_Guy"
    assert (await api.dispatch("GET", "/moderation/records/1", ctx()))["status"] == "expired"


# ---------------------------------------------------------------------------- BOLOs
@pytest.mark.asyncio
async def test_bolo_lifecycle(env):
    api = env["api"]
    b = await api.dispatch("POST", "/bolos", ctx(body={"subject": "Red sedan", "kind": "vehicle", "plate": "abc123", "priority": "critical"}))
    assert b["number"] == 1 and b["plate"] == "ABC123" and b["priority"] == "critical"
    await api.dispatch("POST", "/bolos", ctx(body={"player": "Runner_9", "reason": "Evading", "priority": "low"}))
    listing = await api.dispatch("GET", "/bolos", ctx())
    assert [i["number"] for i in listing["items"]] == [1, 2]  # critical first
    assert listing["counts"]["active"] == 2
    assert (await api.dispatch("GET", "/bolos", ctx(query={"q": "runner"})))["total"] == 1
    await api.dispatch("POST", "/bolos/1/notes", ctx(body={"note": "Seen near postal 210"}))
    await api.dispatch("PATCH", "/bolos/1", ctx(body={"priority": "high"}))
    await api.dispatch("POST", "/bolos/1/close", ctx(body={"reason": "Located"}))
    d = await api.dispatch("GET", "/bolos/1", ctx())
    assert d["status"] == "cleared" and d["close_reason"] == "Located" and d["notes"][0]["note"] == "Seen near postal 210"
    assert [h["event"] for h in d["history"]] == ["created", "note", "updated", "cleared"]
    with pytest.raises(ApiError, match="already closed"):
        await api.dispatch("POST", "/bolos/1/close", ctx(body={}))
    await api.dispatch("POST", "/bolos/1/reopen", ctx())
    with pytest.raises(ApiError) as e:
        await api.dispatch("POST", "/bolos", ctx({"dashboard.view", "bolo.view"}, body={"subject": "x"}))
    assert e.value.status == 403
    await settle()
    notes = await env["p"].notifications.list_for(GUILD, 10, ALL)
    assert any("BOLO #1 created" in n["title"] for n in notes) and any("BOLO #1 reopened" in n["title"] for n in notes)


# ---------------------------------------------------------------------------- staff
@pytest.mark.asyncio
async def test_staff_overview_shifts_and_leave(env):
    api, p = env["api"], env["p"]
    await p.staff.appoint(GUILD, 10, "", 1)
    await p.staff.appoint(GUILD, 11, "", 1)
    await api.dispatch("POST", "/shifts/start", ctx(body={"type": "General"}))
    with pytest.raises(ApiError, match="already have a shift"):
        await api.dispatch("POST", "/shifts/start", ctx(body={"type": "General"}))
    ov = await api.dispatch("GET", "/staff/overview", ctx())
    by = {s["user_id"]: s for s in ov["staff"]}
    assert by["10"]["availability"] == "on_shift" and by["11"]["availability"] == "inactive"
    assert ov["counts"]["on_shift"] == 1
    import datetime as dt
    start = dt.date.today().isoformat()
    end = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    r = await api.dispatch("POST", "/staff/leave/request", ctx(user=11, body={"starts_on": start, "ends_on": end, "reason": "Vacation"}))
    mine = await api.dispatch("GET", "/staff/leave/mine", ctx(user=11))
    assert mine["is_staff"] and mine["items"][0]["status"] == "pending"
    await api.dispatch("POST", f"/staff/leave/{r['id']}", ctx(body={"approve": True}))
    ov = await api.dispatch("GET", "/staff/overview", ctx())
    assert {s["user_id"]: s for s in ov["staff"]}["11"]["availability"] == "on_leave"
    await api.dispatch("POST", f"/staff/leave/{r['id']}/cancel", ctx(user=11))
    with pytest.raises(ApiError):
        await api.dispatch("POST", f"/staff/leave/{r['id']}/cancel", ctx(user=10))  # not your request


# ---------------------------------------------------------------------------- search, overview, isolation
@pytest.mark.asyncio
async def test_search_respects_permissions(env):
    api = env["api"]
    await api.dispatch("POST", "/bolos", ctx(body={"subject": "Blue truck", "priority": "medium"}))
    await env["live"].process(GUILD, snap([("Blue_Player", 5, "Civilian", "Normal")]))
    res = await api.dispatch("GET", "/search", ctx(query={"q": "blue"}))
    assert {r["kind"] for r in res["results"]} >= {"BOLO", "Player"}
    limited = await api.dispatch("GET", "/search", ctx({"dashboard.view"}, query={"q": "blue"}))
    assert limited["results"] == []


@pytest.mark.asyncio
async def test_overview_has_v5_sections_and_hides_by_permission(env):
    api = env["api"]
    full = await api.dispatch("GET", "/overview", ctx())
    assert full["bolos"] == {"active": 0, "urgent": 0} and len(full["trend"]["labels"]) == 14
    assert "health" in full and full["recent"]["in_game"] == []
    limited = await api.dispatch("GET", "/overview", ctx({"dashboard.view"}))
    assert limited["bolos"] is None and limited["recent"]["in_game"] is None and limited["me"] is None


@pytest.mark.asyncio
async def test_v5_data_is_isolated_per_server(env):
    api = env["api"]
    await api.dispatch("POST", "/moderation/records", ctx(body={"action": "note", "player": "Iso_Player", "reason": "x"}))
    await api.dispatch("POST", "/bolos", ctx(body={"subject": "Iso"}))
    other = ctx(guild=2222)
    assert (await api.dispatch("GET", "/moderation/records", other))["total"] == 0
    assert (await api.dispatch("GET", "/bolos", other))["total"] == 0
    with pytest.raises(ApiError) as e:
        await api.dispatch("GET", "/bolos/1", other)
    assert e.value.status == 404


@pytest.mark.asyncio
async def test_activity_prune(env):
    db = env["db"]
    await activity.record(db, GUILD, "shifts", "old")
    await db.execute("UPDATE activity_events SET at = '2000-01-01T00:00:00.000000Z'")
    await activity.record(db, GUILD, "shifts", "new")
    assert await activity.prune(db, GUILD) == 1
    assert await db.fetchval("SELECT COUNT(*) FROM activity_events") == 1


# ---------------------------------------------------------------------------- sync loop integration
@pytest.mark.asyncio
async def test_sync_loop_drops_refused_extras_and_handles_offline_server(env, tmp_path):
    import asyncio

    from erlc.client import ERLCError
    from erlc.synchronization import SyncManager
    from security.secrets import SecretStore

    calls = []

    class Client:
        rate = type("R", (), {"remaining": None, "reset": None})()

        def __init__(self):
            self.mode = "extras_refused"

        async def get_server(self, **flags):
            calls.append(sorted(k for k, v in flags.items() if v and k not in {"players", "emergency_calls"}))
            if self.mode == "offline":
                raise ERLCError("offline", 422, 3002)
            if self.mode == "extras_refused" and "Staff" in flags:
                raise ERLCError("restricted", 403, 9998)
            return snap([("Alice", 1, "Police", "Normal")])

    class Intake:
        async def process_snapshot(self, *a, **k):
            return type("Res", (), {"new_incidents": [], "updated_incidents": [], "ended_calls": 0})()

    client = Client()
    sm = SyncManager(env["db"], env["core"].bus, SecretStore(env["db"], "", tmp_path / "k"), lambda: None, lambda: Intake(),
                     base_url="x", env_server_key="k", env_global_key="", env_guild_id=0, poll_seconds=1)
    sm.live = env["live"]

    async def fake_client(gs):
        return client
    sm._client_for = fake_client
    sm.start_guild(GUILD)
    await asyncio.sleep(0.2)
    assert calls[0] and calls[1] == []  # first try with extras, refused -> retried without, polling carried on
    assert env["live"].state(GUILD).baseline and env["live"].state(GUILD).extras_error
    client.mode = "offline"
    sm.trigger(GUILD)
    await asyncio.sleep(1.3)
    assert env["live"].state(GUILD).offline
    await sm.stop()


@pytest.mark.asyncio
async def test_activity_feed_only_shows_what_you_may_see(env):
    p, api = env["p"], env["api"]
    await p.log(GUILD, "moderation", "Ban issued", fields=[("Member", "<@10>", True)])
    await p.log(GUILD, "shifts", "Shift Started")
    viewer = await api.dispatch("GET", "/activity", ctx({"dashboard.view", "shift.self"}))
    assert [i["title"] for i in viewer["items"]] == ["Shift Started"]
    assert {g["key"] for g in viewer["groups"]} == {"shifts"}
    assert (await api.dispatch("GET", "/activity", ctx({"dashboard.view", "shift.self"}, query={"group": "moderation"})))["total"] == 0
    full = await api.dispatch("GET", "/activity", ctx())
    assert "@member" in full["items"][-1]["body"] or "@" in full["items"][-1]["body"]  # mentions are made readable
    limited = await api.dispatch("GET", "/overview", ctx({"dashboard.view"}))
    assert limited["recent"]["feed"] == []
