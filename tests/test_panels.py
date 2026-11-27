"""Boots the real DispatchBot offline (no Discord login) and validates every panel,
persistent view, modal and slash command against Discord's API limits."""
import inspect

import discord
import pytest
import pytest_asyncio

from config import Settings
from tests.conftest import GUILD, settle


@pytest_asyncio.fixture
async def bot(tmp_path, monkeypatch):
    from bot.client import DispatchBot
    s = Settings()
    s.discord_token = "test"
    s.dev_guild_id = 0
    s.data_dir = tmp_path
    s.database_path = tmp_path / "bot.db"
    s.backup_dir = tmp_path / "backups"
    s.log_dir = tmp_path / "logs"
    s.piper_voice_dir = tmp_path / "voices"
    s.webhook_enabled = False
    s.gemini_api_key = ""
    s.erlc_server_key = ""
    s.dashboard_enabled = False  # v4.0: don't bind the web dashboard port during tests
    b = DispatchBot(s)

    async def no_sync(*a, **k):
        return []

    monkeypatch.setattr(b.tree, "sync", no_sync)
    await b.setup_hook()
    await b.core.units.ensure_defaults(GUILD, "Test")
    await b.ai.ensure_guild(GUILD)
    yield b
    for t in b._tasks:
        t.cancel()
    b.loop_monitor.stop()
    if b.voice._watchdog:
        b.voice._watchdog.cancel()
    if b.voice_commands._task:
        b.voice_commands._task.cancel()
    if b.http_session:
        await b.http_session.close()
    await b.db.close()


def check_embed(e: discord.Embed):
    d = e.to_dict()
    assert len(d.get("title", "")) <= 256
    assert len(d.get("description", "")) <= 4096
    assert len(d.get("fields", [])) <= 25
    for f in d.get("fields", []):
        assert 0 < len(f["name"]) <= 256, f
        assert 0 < len(f["value"]) <= 1024, f
    assert len(e) <= 6000


def check_view(view: discord.ui.View):
    comps = view.to_components()
    assert len(comps) <= 5
    for item in view.children:
        cid = getattr(item, "custom_id", None)
        if cid:
            assert len(cid) <= 100
        if isinstance(item, discord.ui.Button):
            assert item.label is None or len(item.label) <= 80
        if isinstance(item, discord.ui.Select):
            assert 1 <= len(item.options) <= 25
            for o in item.options:
                assert len(o.label) <= 100 and (o.description is None or len(o.description) <= 100)


async def populate(b):
    core = b.core
    await core.units.register(GUILD, 1, "Officer One", "1A-01", "PD")
    await core.units.register(GUILD, 2, "Medic Two", "M-2", "EMS")
    o = await core.units.officer_by_discord(GUILD, 1)
    await core.assignments.set_unit_status(o["unit_id"], "10-8", 1)
    for i in range(14):
        inc = await core.create_test_call(GUILD, 1, location=f"Location {i} " + "x" * 120,
                                          description="multi vehicle crash with injuries " * 10)
    await core.assignments.attach(inc["id"], o["unit_id"], 1)
    await core.incidents.add_note(inc["id"], 1, "Suspect fled north " * 30, author="1A-01")
    await core.bolos.create(GUILD, 1, kind="vehicle", subject="Red sedan " * 8, description="d" * 700, plate="ABC123")
    await core.reports.create(GUILD, 3, rp_name="Jo", location="Main", category="Theft", occurred_at="today",
                              description="Someone stole my bike " * 50)
    await core.assignments.request_backup(GUILD, o["unit_id"], inc["id"], 1, "emergency", "need help")
    await settle()


@pytest.mark.asyncio
async def test_every_panel_renders_within_discord_limits(bot):
    await populate(bot)
    assert set(bot.panels.builders) == {"dashboard", "incidents", "units", "voice", "citizen", "reports", "bolos", "ai", "erlc",
                                        "admin", "stats"}
    for name in bot.panels.builders:
        embeds, view = await bot.panels.render(GUILD, name)
        assert embeds, name
        for e in embeds:
            check_embed(e)
        if view:
            check_view(view)


@pytest.mark.asyncio
async def test_empty_guild_panels_render(bot):
    for name in bot.panels.builders:
        embeds, view = await bot.panels.render(GUILD, name)
        for e in embeds:
            check_embed(e)
        if view:
            check_view(view)


@pytest.mark.asyncio
async def test_incident_embed_and_dynamic_actions(bot):
    from bot.ui import incident_embed
    from panels.incidents import ACTION_STYLE, IncidentAction
    await populate(bot)
    inc = (await bot.core.incidents.list_active(GUILD))[0]
    units = await bot.core.incidents.assigned_units(inc["id"])
    check_embed(incident_embed(inc, units, notes=await bot.core.incidents.notes(inc["id"])))
    view = discord.ui.View(timeout=None)
    for a in list(ACTION_STYLE)[:25]:
        view.add_item(IncidentAction(a, 123456789))
    assert all(len(c.custom_id) <= 100 for c in view.children)
    import re
    m = re.fullmatch(IncidentAction.__discord_ui_compiled_template__, "cad:i:attachme:42")
    assert m and m["id"] == "42"


@pytest.mark.asyncio
async def test_persistent_views_registered(bot):
    persistent = bot._connection._view_store.persistent_views
    ids = {c.custom_id for v in persistent for c in v.children if getattr(c, "custom_id", None)}
    for cid in ("cad:incidents:select", "cad:units:status", "cad:voice:join", "cad:cit:911", "cad:erlc:key", "cad:ai:primary",
                "cad:admin:section", "cad:stats:shift", "cad:bolo:create", "cad:rep:select", "cad:dash:avail"):
        assert cid in ids, cid


def _modal_classes():
    import panels.administration as adm
    import panels.bolos as bol
    import panels.citizen_services as cit
    import panels.erlc as er
    import panels.incidents as inc
    import panels.reports as rep
    import panels.units as un
    for mod in (adm, bol, cit, er, inc, rep, un):
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, discord.ui.Modal) and cls is not discord.ui.Modal and cls.__module__ == mod.__name__:
                yield cls


@pytest.mark.asyncio
async def test_all_modals_respect_limits(bot):
    deps = await bot.core.units.departments(GUILD)
    inc = await bot.core.create_test_call(GUILD, 1, location="x" * 140, description="y" * 900)
    samples = {
        "RegisterModal": lambda c: c(None, deps), "ContactModal": lambda c: c(deps, True), "UpdateIncidentModal": lambda c: c(inc),
        "BoloModal": lambda c: c(None), "PriorityReasonModal": lambda c: c(1, "high"),
        "DispatchSettingsModal": None,
    }
    count = 0
    for cls in _modal_classes():
        if cls.__name__ == "DispatchSettingsModal":
            m = await cls.create(bot, GUILD)
        elif cls.__name__ in samples:
            m = samples[cls.__name__](cls)
        else:
            if cls.__init__ is discord.ui.Modal.__init__:
                m = cls()
            else:
                params = [p for p in inspect.signature(cls.__init__).parameters.values()
                          if p.name != "self" and p.kind == p.POSITIONAL_OR_KEYWORD]
                m = cls(*([1] * len(params)))
        assert len(m.title) <= 45, cls.__name__
        assert 1 <= len(m.children) <= 5, cls.__name__
        for child in m.children:
            assert len(child.label) <= 45, (cls.__name__, child.label)
            if child.default:
                assert len(child.default) <= child.max_length, (cls.__name__, child.label)
        count += 1
    assert count >= 20


@pytest.mark.asyncio
async def test_slash_commands_valid(bot):
    names = set()
    for cmd in bot.tree.get_commands():
        payload = cmd.to_dict(bot.tree)
        assert 1 <= len(payload["name"]) <= 32
        assert 1 <= len(payload.get("description", "x")) <= 100
        names.add(payload["name"])
    for required in ("setup", "911", "unit", "status", "attach", "detach", "calls", "incident", "backup", "bolo", "dispatch",
                     "voice", "ai", "stats",
                     # v4.0 Command Center
                     "verify", "verification", "staff", "shift", "session", "apply", "application", "warn", "ban", "ask"):
        assert required in names, required


@pytest.mark.asyncio
async def test_panel_manager_skips_unchanged_edits(bot):
    class FakeMsg:
        edits = 0

        async def edit(self, **kw):
            FakeMsg.edits += 1

    class FakeChannel:
        id = 55

        def get_partial_message(self, mid):
            return FakeMsg()

    bot._connection._get_guild = lambda gid: None
    await bot.db.execute("INSERT INTO panels(guild_id, panel, channel_id, message_id) VALUES (?, 'citizen', 55, 66)", (GUILD,))
    orig = bot.get_channel
    bot.get_channel = lambda cid: FakeChannel() if cid == 55 else orig(cid)
    assert await bot.panels.refresh(GUILD, "citizen")
    assert await bot.panels.refresh(GUILD, "citizen")
    assert FakeMsg.edits == 1  # second refresh had identical content -> no API call
