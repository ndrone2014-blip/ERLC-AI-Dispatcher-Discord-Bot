import logging

import pytest

from core.logging_setup import RedactingFilter, register_secret
from security.permissions import Actor, PermissionManager
from security.secrets import SecretStore
from security.validation import (RateLimiter, ValidationError, clean_text, digest, is_duplicate_submission,
                                 parse_incident_number, validate_callsign)
from tests.conftest import GUILD


@pytest.mark.asyncio
async def test_permission_groups(db):
    pm = PermissionManager(db)
    await pm.set_group_roles(GUILD, "officer", [10])
    await pm.set_group_roles(GUILD, "supervisor", [20])
    citizen = Actor(1, set())
    officer = Actor(2, {10})
    supervisor = Actor(3, {20})
    admin = Actor(4, set(), is_discord_admin=True)
    owner = Actor(5, set(), is_owner=True)
    assert await pm.has(GUILD, citizen, "call.submit")
    assert not await pm.has(GUILD, citizen, "incident.attach_self")
    assert await pm.has(GUILD, officer, "incident.attach_self")
    assert not await pm.has(GUILD, officer, "incident.priority_approve")
    assert await pm.has(GUILD, supervisor, "incident.priority_approve")
    assert not await pm.has(GUILD, supervisor, "admin.erlc")
    assert await pm.has(GUILD, admin, "admin.erlc")
    assert await pm.has(GUILD, owner, "anything.at.all")
    assert not await pm.has(GUILD, officer, "report.view_all")
    await pm.set_override(GUILD, "report.view_all", ["officer"])
    assert await pm.has(GUILD, officer, "report.view_all")


@pytest.mark.asyncio
async def test_secret_store_encrypts(db, tmp_path):
    store = SecretStore(db, "", tmp_path / "secret.key")
    await store.set(GUILD, "erlc_server_key", "super-secret-value-123")
    raw = await db.fetchval("SELECT ciphertext FROM secrets")
    assert "super-secret" not in raw
    assert await store.get(GUILD, "erlc_server_key") == "super-secret-value-123"
    # a new store with the same key file can decrypt (restart)
    store2 = SecretStore(db, "", tmp_path / "secret.key")
    assert await store2.get(GUILD, "erlc_server_key") == "super-secret-value-123"


def test_log_redaction():
    register_secret("my-super-secret-token-value")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "token is my-super-secret-token-value and server-key: abcdef123", (), None)
    RedactingFilter().filter(rec)
    msg = rec.getMessage()
    assert "my-super-secret" not in msg and "abcdef123" not in msg


def test_validation():
    assert clean_text("  hi @everyone ") == "hi @​everyone"
    assert validate_callsign("1a-12") == "1A-12"
    with pytest.raises(ValidationError):
        validate_callsign("bad<script>")
    assert parse_incident_number("#104") == 104
    assert parse_incident_number("INC-7") == 7
    with pytest.raises(ValidationError):
        parse_incident_number("abc")
    rl = RateLimiter(2, 60)
    assert rl.check("u") is None and rl.check("u") is None and rl.check("u") is not None


@pytest.mark.asyncio
async def test_duplicate_submission_guard(db):
    d = digest("Main St", "fire")
    assert not await is_duplicate_submission(db, GUILD, 1, "911", d)
    assert await is_duplicate_submission(db, GUILD, 1, "911", d)
    assert not await is_duplicate_submission(db, GUILD, 2, "911", d)
