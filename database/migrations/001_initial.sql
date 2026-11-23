-- Initial schema for the ER:LC AI Dispatch system.
-- Every operational table is scoped by guild_id so multiple Discord servers stay separate.

CREATE TABLE IF NOT EXISTS guilds (
    guild_id    INTEGER PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Flexible per-guild settings stored as JSON values.
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id    INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, key)
);

-- Encrypted credentials (ER:LC server key etc). Never stored in plaintext.
CREATE TABLE IF NOT EXISTS secrets (
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    ciphertext  TEXT NOT NULL,
    updated_by  INTEGER,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS counters (
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    value       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    short_code  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'law',   -- law | fire | ems | supervisor | dispatch | other
    role_id     INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, short_code)
);

CREATE TABLE IF NOT EXISTS status_codes (
    guild_id     INTEGER NOT NULL,
    code         TEXT NOT NULL,
    label        TEXT NOT NULL,
    availability TEXT NOT NULL,                -- available | assigned | unavailable | off
    sort         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, code)
);

CREATE TABLE IF NOT EXISTS units (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    callsign            TEXT NOT NULL,
    department_id       INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    status_code         TEXT NOT NULL DEFAULT '10-7',
    current_incident_id INTEGER REFERENCES incidents(id) ON DELETE SET NULL,
    status_changed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, callsign)
);

CREATE TABLE IF NOT EXISTS officers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    discord_id      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    roblox_username TEXT,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    unit_id         INTEGER REFERENCES units(id) ON DELETE SET NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS unit_status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    unit_id     INTEGER NOT NULL,
    officer_id  INTEGER,
    old_code    TEXT,
    new_code    TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS incidents (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id                INTEGER NOT NULL,
    number                  INTEGER NOT NULL,
    source                  TEXT NOT NULL,        -- erlc | discord | manual | test
    verification            TEXT NOT NULL,        -- verified | unverified | test
    status                  TEXT NOT NULL DEFAULT 'pending',  -- pending | active | closed
    category                TEXT,
    nature                  TEXT,
    description             TEXT,
    location                TEXT,
    postal                  TEXT,
    street                  TEXT,
    position_x              REAL,
    position_z              REAL,
    caller_display          TEXT,
    requested_department    TEXT,
    erlc_call_number        INTEGER,
    erlc_started_at         INTEGER,
    erlc_team               TEXT,
    ai_priority             TEXT,
    priority                TEXT,
    priority_status         TEXT NOT NULL DEFAULT 'recommended', -- recommended | approved | overridden
    priority_approved_by    INTEGER,
    priority_approved_at    TEXT,
    priority_reason         TEXT,
    recommended_units       INTEGER,
    recommended_departments TEXT,                 -- JSON list of department short codes
    supervisor_recommended  INTEGER NOT NULL DEFAULT 0,
    recommendation_reason   TEXT,
    assessment_source       TEXT,
    assessment_json         TEXT,
    created_by              INTEGER,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    closed_at               TEXT,
    closed_by               INTEGER,
    close_reason            TEXT,
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS idx_incidents_guild_status ON incidents(guild_id, status);

CREATE TABLE IF NOT EXISTS incident_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor_id    INTEGER,
    event_type  TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_incident_history ON incident_history(incident_id);

CREATE TABLE IF NOT EXISTS incident_assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    unit_id     INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    assigned_by INTEGER,
    assigned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    cleared_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignments_open ON incident_assignments(incident_id, cleared_at);

CREATE TABLE IF NOT EXISTS erlc_calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    call_number         INTEGER NOT NULL,
    started_at          INTEGER NOT NULL,
    team                TEXT,
    caller_id           INTEGER,
    caller_name         TEXT,
    description         TEXT,
    position_descriptor TEXT,
    position_x          REAL,
    position_z          REAL,
    players             TEXT,
    raw                 TEXT,
    first_seen          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    gone_at             TEXT,
    incident_id         INTEGER REFERENCES incidents(id) ON DELETE SET NULL,
    UNIQUE (guild_id, call_number, started_at)
);

CREATE TABLE IF NOT EXISTS backup_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    incident_id  INTEGER REFERENCES incidents(id) ON DELETE SET NULL,
    unit_id      INTEGER REFERENCES units(id) ON DELETE SET NULL,
    requested_by INTEGER,
    urgency      TEXT NOT NULL DEFAULT 'routine',
    note         TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_at  TEXT,
    resolved_by  INTEGER
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    reporter_id  INTEGER NOT NULL,
    rp_name      TEXT,
    location     TEXT,
    category     TEXT,
    occurred_at  TEXT,
    description  TEXT NOT NULL,
    suspects     TEXT,
    witnesses    TEXT,
    attachments  TEXT,
    status       TEXT NOT NULL DEFAULT 'submitted', -- submitted | assigned | in_review | supervisor_review | closed
    assigned_to  INTEGER,
    summary      TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, number)
);

CREATE TABLE IF NOT EXISTS report_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    report_id   INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor_id    INTEGER,
    event_type  TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS bolos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other',   -- person | vehicle | other
    subject     TEXT NOT NULL,
    description TEXT,
    plate       TEXT,
    last_seen   TEXT,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | cleared
    created_by  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    cleared_by  INTEGER,
    cleared_at  TEXT,
    UNIQUE (guild_id, number)
);

CREATE TABLE IF NOT EXISTS bolo_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    bolo_id     INTEGER NOT NULL REFERENCES bolos(id) ON DELETE CASCADE,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor_id    INTEGER,
    event_type  TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS ai_providers (
    guild_id        INTEGER NOT NULL,
    provider        TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    model           TEXT,
    priority_order  INTEGER NOT NULL DEFAULT 100,
    last_ok_at      TEXT,
    last_error_at   TEXT,
    last_error      TEXT,
    cooldown_until  REAL,
    PRIMARY KEY (guild_id, provider)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    provider    TEXT NOT NULL,
    model       TEXT,
    purpose     TEXT,
    ok          INTEGER NOT NULL,
    latency_ms  INTEGER,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    error       TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_usage ON ai_usage(provider, at);

CREATE TABLE IF NOT EXISTS announcements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    incident_id  INTEGER,
    text         TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 1,     -- higher plays first
    critical     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'queued', -- queued | playing | played | failed | cancelled | muted
    dedupe_key   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    played_at    TEXT,
    UNIQUE (guild_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_announcements_queue ON announcements(guild_id, status, priority);

CREATE TABLE IF NOT EXISTS panels (
    guild_id    INTEGER NOT NULL,
    panel       TEXT NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, panel)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    target      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log(guild_id, at);

CREATE TABLE IF NOT EXISTS api_sync_state (
    guild_id             INTEGER PRIMARY KEY,
    connected            INTEGER NOT NULL DEFAULT 0,
    last_success_at      TEXT,
    last_attempt_at      TEXT,
    last_error_at        TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    server_name          TEXT,
    current_players      INTEGER,
    max_players          INTEGER,
    join_key             TEXT,
    rate_limit_remaining INTEGER,
    rate_limit_reset     INTEGER,
    last_call_seen_at    TEXT,
    webhook_last_at      TEXT,
    webhook_last_status  TEXT
);

CREATE TABLE IF NOT EXISTS submission_guard (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    digest      TEXT NOT NULL,
    at          REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind, digest)
);
