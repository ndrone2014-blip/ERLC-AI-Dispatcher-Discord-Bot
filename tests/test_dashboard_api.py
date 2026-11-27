"""v4.0 web dashboard API: permissions, server isolation and routes (no aiohttp, no Discord)."""
import pytest
import pytest_asyncio

from community.actions import RecordingActions
from community.platform import CommunityPlatform
from config import Settings
from security.audit import AuditLog
from security.permissions import DEFAULT_PERMISSIONS, PermissionManager
from tests.conftest import GUILD, settle
from web.api import NAV, ROUTES, Api, ApiError, CsvResponse, Ctx

OTHER = 2222
ALL = set(DEFAULT_PERMISSIONS)


class FakeAI:
    providers = {"groq": object()}

    def active_provider(self, gid):
        return None

    async def guild_providers(self, gid):
        return []

    async def ai_settings(self, gid):
        return {"mode": "manual", "failover": True, "preferred": None}

    async def usage_summary(self, gid):
        return {}


class FakeLogbook:
    def __init__(self, db):
        self.db = db
        self.s = {"categories": {}, "channels": {}}

    async def settings(self, gid):
        return self.s

    async def set_category(self, gid, k, v):
        self.s["categories"][k] = v

    async def set_category_channel(self, gid, k, cid):
        self.s["channels"][k] = cid

    async def write(self, *a, **k):
        pass


class FakeBot:
    def __init__(self, db, core, community):
        self.db, self.core, self.community = db, core, community
        self.ai = FakeAI()
        self.bus = core.bus
        self.permissions = community.permissions
        self.logbook = FakeLogbook(db)
        self.settings = Settings()
        self.started_at_iso = None
        self.latency = float("nan")

    def get_guild(self, gid):
        return None

    async def activity_feed(self, gid):
        return []


@pytest_asyncio.fixture
async def api(db, core):
    p = CommunityPlatform(db, core.bus, AuditLog(db), PermissionManager(db), actions=RecordingActions(), dispatch=core)
    yield Api(FakeBot(db, core, p))
    await settle()


def ctx(perms=ALL, guild=GUILD, user=10, **kw):
    return Ctx(guild_id=guild, user_id=user, perms=set(perms), username="tester", **kw)


def test_routes_and_nav_use_real_permissions():
    for _m, _p, handler, perm in ROUTES:
        assert perm is None or perm in DEFAULT_PERMISSIONS, (handler, perm)
    for _g, items in NAV:
        for _path, _label, perm in items:
            for p in (perm if isinstance(perm, tuple) else (perm,)):
                assert p in DEFAULT_PERMISSIONS, p
    assert len({(m, p) for m, p, _h, _x in ROUTES}) == len(ROUTES), "duplicate route"


@pytest.mark.asyncio
async def test_permission_is_checked_per_route(api):
    with pytest.raises(ApiError) as e:
        await api.dispatch("GET", "/moderation", ctx({"dashboard.view"}))
    assert e.value.status == 403
    with pytest.raises(ApiError) as e:
        await api.dispatch("GET", "/nope", ctx())
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:
        await api.dispatch("DELETE", "/overview", ctx())
    assert e.value.status == 405


@pytest.mark.asyncio
async def test_overview_hides_sections_without_permission(api):
    full = await api.dispatch("GET", "/overview", ctx())
    assert full["recent"]["moderation"] == [] and full["system"]["latency_ms"] is None
    limited = await api.dispatch("GET", "/overview", ctx({"dashboard.view"}))
    assert limited["recent"]["moderation"] is None and limited["recent"]["audit"] is None
    meta = await api.dispatch("GET", "/meta", ctx({"dashboard.view", "members.view"}))
    paths = {i["path"] for g in meta["nav"] for i in g["items"]}
    assert "members" in paths and "moderation" not in paths and "settings" not in paths


@pytest.mark.asyncio
async def test_moderation_via_dashboard_needs_action_permission_and_confirmation(api):
    with pytest.raises(ApiError) as e:
        await api.dispatch("POST", "/moderation", ctx({"mod.view", "mod.warn"}, body={"action": "ban", "user_id": "20", "reason": "x"}))
    assert e.value.status == 403
    with pytest.raises(ApiError, match="Confirm"):
        await api.dispatch("POST", "/moderation", ctx(body={"action": "ban", "user_id": "20", "reason": "raid"}))
    case = await api.dispatch("POST", "/moderation", ctx(body={"action": "ban", "user_id": "20", "reason": "raid", "confirm": True}))
    assert case["number"] == 1 and case["source"] == "dashboard" and case["target_id"] == "20"
    listing = await api.dispatch("GET", "/moderation", ctx(query={"action": "ban"}))
    assert listing["total"] == 1
    with pytest.raises(ApiError) as e:  # service errors become clean 400s
        await api.dispatch("POST", "/moderation", ctx(body={"action": "timeout", "user_id": "20", "reason": "x", "duration": "forever"}))
    assert e.value.status == 400


@pytest.mark.asyncio
async def test_servers_never_see_each_others_data(api):
    await api.c.moderation.create(OTHER, "warn", 30, 99, reason="other server")
    await api.c.members.bulk_sync(OTHER, [{"id": 30, "name": "carl"}])
    assert (await api.dispatch("GET", "/moderation", ctx()))["total"] == 0
    assert (await api.dispatch("GET", "/members", ctx()))["total"] == 0
    with pytest.raises(ApiError) as e:
        await api.dispatch("GET", "/moderation/1", ctx())
    assert e.value.status == 404
    # notifications created for another server can't be marked read from this one
    nid = await api.c.notifications.create(OTHER, "system", "x")
    assert (await api.dispatch("POST", "/notifications/read", ctx(body={"ids": [nid]})))["marked"] == 0


@pytest.mark.asyncio
async def test_dispatch_center_flow(api, core):
    inc = await api.dispatch("POST", "/incidents", ctx(body={"nature": "Bank robbery", "location": "Central Bank"}))
    assert inc["number"] == 1 and inc["status"] != "closed"
    await settle()
    board = await api.dispatch("GET", "/dispatch", ctx())
    assert board["incidents"][0]["nature"] == "Bank robbery" and "New" in board["workflow_statuses"]
    upd = await api.dispatch("PATCH", "/incidents/1", ctx(body={"workflow_status": "In Progress", "location": "Central Bank vault"}))
    assert upd["workflow_status"] == "In Progress" and upd["location"] == "Central Bank vault"
    with pytest.raises(ApiError):
        await api.dispatch("PATCH", "/incidents/1", ctx(body={"workflow_status": "Made up"}))
    await api.dispatch("POST", "/incidents/1/note", ctx(body={"note": "Suspect on foot"}))
    detail = await api.dispatch("GET", "/incidents/1", ctx())
    assert any(h["event_type"] == "note" for h in detail["history"])
    await api.dispatch("POST", "/incidents/1/close", ctx(body={"reason": "Done"}))
    hist = await api.dispatch("GET", "/incidents", ctx(query={"status": "closed"}))
    assert hist["total"] == 1


@pytest.mark.asyncio
async def test_logs_export_and_settings(api):
    await api.c.log(GUILD, "moderation", "Something happened", description="detail")
    logs = await api.dispatch("GET", "/logs", ctx(query={"category": "moderation"}))
    assert logs["total"] == 1 and "moderation" in logs["categories"]
    with pytest.raises(ApiError):
        await api.dispatch("GET", "/logs/export.csv", ctx({"logs.view"}))
    out = await api.dispatch("GET", "/logs/export.csv", ctx())
    assert isinstance(out, CsvResponse) and "Something happened" in out.text


@pytest.mark.asyncio
async def test_staff_shift_and_assistant_endpoints(api):
    await api.dispatch("POST", "/staff", ctx(body={"user_id": "10", "rank": "Moderator"}))
    data = await api.dispatch("GET", "/staff", ctx())
    assert data["total"] == 1 and data["items"][0]["user_id"] == "10"
    await api.dispatch("POST", "/shifts/start", ctx(body={"type": "Patrol"}))
    with pytest.raises(ApiError) as e:  # ending someone else's shift needs shift.manage
        await api.dispatch("POST", "/shifts/end", ctx({"shift.self"}, user=20, body={"user_id": "10"}))
    assert e.value.status == 403
    r = await api.dispatch("POST", "/assistant", ctx(body={"question": "who is on shift?"}))
    assert r["intent"] == "staff_on_shift" and "1 member" in r["text"]


@pytest.mark.asyncio
async def test_settings_are_validated(api):
    with pytest.raises(ApiError):
        await api.dispatch("PUT", "/dispatch/settings", ctx(body={"settings": {"not_a_setting": 1}}))
    with pytest.raises(ApiError):
        await api.dispatch("PUT", "/settings/channels", ctx(body={"evil_key": "1"}))
    perms = await api.dispatch("PUT", "/settings/permissions/overrides/mod.warn", ctx(body={"groups": ["moderator"]}))
    row = next(p for p in perms["permissions"] if p["key"] == "mod.warn")
    assert row["overridden"] and row["groups"] == ["moderator"]
    s = await api.dispatch("PUT", "/bot/appearance", ctx(body={"accent": "#FF0000", "footer_note": "LCPD"}))
    assert s["accent_hex"] == "#FF0000"
