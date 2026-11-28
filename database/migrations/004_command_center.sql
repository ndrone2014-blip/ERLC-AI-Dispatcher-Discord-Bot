-- Hive Dispatch | ERLC - v4.0.0 "Command Center" update
-- Community management platform: members, Roblox verification, staff, shifts, sessions, applications,
-- moderation, automation, notifications, embed templates, analytics and the web dashboard.
-- Every table is scoped by guild_id so servers never see each other's data.

-- ---------------------------------------------------------------- members
CREATE TABLE IF NOT EXISTS members (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    username      TEXT,
    display_name  TEXT,
    avatar_url    TEXT,
    is_bot        INTEGER NOT NULL DEFAULT 0,
    roles         TEXT,                 -- JSON list of role ids
    joined_at     TEXT,
    left_at       TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_name ON members(guild_id, username);

CREATE TABLE IF NOT EXISTS member_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,            -- join | leave
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_member_events ON member_events(guild_id, at);

CREATE TABLE IF NOT EXISTS member_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_member_notes ON member_notes(guild_id, user_id);

-- ---------------------------------------------------------------- Roblox verification
CREATE TABLE IF NOT EXISTS roblox_links (
    guild_id         INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
    roblox_id        INTEGER NOT NULL,
    roblox_username  TEXT NOT NULL,
    display_name     TEXT,
    method           TEXT NOT NULL,     -- profile_code | oauth | manual
    verified_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    verified_by      INTEGER,           -- staff member for manual links
    PRIMARY KEY (guild_id, discord_id),
    UNIQUE (guild_id, roblox_id)
);

CREATE TABLE IF NOT EXISTS verification_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
    roblox_id        INTEGER,
    roblox_username  TEXT,
    method           TEXT NOT NULL,     -- profile_code | oauth
    code             TEXT,              -- phrase the member places in their Roblox profile
    token            TEXT,              -- OAuth state / link token (random, single use)
    status           TEXT NOT NULL DEFAULT 'pending', -- pending | verified | failed | expired | cancelled
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at       TEXT NOT NULL,
    completed_at     TEXT,
    failure          TEXT
);
CREATE INDEX IF NOT EXISTS idx_verif_req ON verification_requests(guild_id, discord_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verif_token ON verification_requests(token) WHERE token IS NOT NULL;

CREATE TABLE IF NOT EXISTS verification_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
    roblox_id        INTEGER,
    roblox_username  TEXT,
    action           TEXT NOT NULL,     -- verified | removed | manual_link | failed | role_sync
    method           TEXT,
    actor_id         INTEGER,
    detail           TEXT,
    at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_verif_hist ON verification_history(guild_id, at);

-- ---------------------------------------------------------------- staff
CREATE TABLE IF NOT EXISTS staff (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    rank           TEXT NOT NULL DEFAULT 'Staff',
    department     TEXT,
    status         TEXT NOT NULL DEFAULT 'active',  -- active | leave | removed
    appointed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    appointed_by   INTEGER,
    removed_at     TEXT,
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS staff_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    action     TEXT NOT NULL,   -- appointed | promoted | demoted | updated | leave_start | leave_end | removed | reinstated
    from_rank  TEXT,
    to_rank    TEXT,
    actor_id   INTEGER,
    reason     TEXT,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_staff_history ON staff_history(guild_id, user_id);

CREATE TABLE IF NOT EXISTS staff_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_staff_notes ON staff_notes(guild_id, user_id);

CREATE TABLE IF NOT EXISTS leave_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    starts_on   TEXT NOT NULL,
    ends_on     TEXT NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied | cancelled
    reviewed_by INTEGER,
    reviewed_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_leave ON leave_requests(guild_id, status);

CREATE TABLE IF NOT EXISTS shifts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    shift_type  TEXT NOT NULL DEFAULT 'General',
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at    TEXT,
    duration_s  INTEGER,
    notes       TEXT,
    ended_by    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shifts_open ON shifts(guild_id, ended_at);
CREATE INDEX IF NOT EXISTS idx_shifts_user ON shifts(guild_id, user_id, started_at);
-- At most one open shift per member.
CREATE UNIQUE INDEX IF NOT EXISTS idx_shifts_one_open ON shifts(guild_id, user_id) WHERE ended_at IS NULL;

-- ---------------------------------------------------------------- community sessions
CREATE TABLE IF NOT EXISTS community_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    number           INTEGER NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT,
    host_id          INTEGER,
    department       TEXT,
    scheduled_for    TEXT,
    started_at       TEXT,
    ended_at         TEXT,
    status           TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | active | ended | cancelled
    notes            TEXT,
    reminder_minutes INTEGER NOT NULL DEFAULT 15,
    reminded         INTEGER NOT NULL DEFAULT 0,
    channel_id       INTEGER,
    message_id       INTEGER,
    created_by       INTEGER,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON community_sessions(guild_id, status);

CREATE TABLE IF NOT EXISTS session_participants (
    session_id INTEGER NOT NULL REFERENCES community_sessions(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    joined_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (session_id, user_id)
);

-- ---------------------------------------------------------------- applications
CREATE TABLE IF NOT EXISTS application_forms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'custom',
    description     TEXT,
    questions       TEXT NOT NULL DEFAULT '[]',  -- JSON list of {id,label,type,required,options,max_length}
    enabled         INTEGER NOT NULL DEFAULT 1,
    submit_roles    TEXT NOT NULL DEFAULT '[]',  -- empty = everyone
    reviewer_roles  TEXT NOT NULL DEFAULT '[]',  -- empty = application reviewers permission
    approve_role_id INTEGER,                    -- role given when approved (optional)
    review_channel_id INTEGER,                  -- where new submissions are posted (optional)
    cooldown_hours  INTEGER NOT NULL DEFAULT 24,
    require_verified INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    number          INTEGER NOT NULL,
    form_id         INTEGER REFERENCES application_forms(id) ON DELETE SET NULL,
    form_name       TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    answers         TEXT NOT NULL,              -- JSON list of {question, answer}
    status          TEXT NOT NULL DEFAULT 'submitted', -- submitted | under_review | awaiting_response | approved | denied | archived
    reviewer_id     INTEGER,
    decision_reason TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS idx_applications ON applications(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(guild_id, user_id);

CREATE TABLE IF NOT EXISTS application_notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    author_id      INTEGER NOT NULL,
    note           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------- moderation
CREATE TABLE IF NOT EXISTS mod_cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    number        INTEGER NOT NULL,
    action        TEXT NOT NULL,     -- warn | timeout | kick | ban | unban | note
    target_id     INTEGER NOT NULL,
    target_name   TEXT,
    moderator_id  INTEGER NOT NULL,
    reason        TEXT,
    evidence      TEXT,
    duration_s    INTEGER,
    expires_at    TEXT,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | revoked | failed
    outcome       TEXT,              -- result of the Discord action (e.g. "DM delivered", error text)
    source        TEXT NOT NULL DEFAULT 'discord',  -- discord | dashboard
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS idx_mod_cases_target ON mod_cases(guild_id, target_id);
CREATE INDEX IF NOT EXISTS idx_mod_cases_created ON mod_cases(guild_id, created_at);

CREATE TABLE IF NOT EXISTS member_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    reporter_id  INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    evidence     TEXT,
    status       TEXT NOT NULL DEFAULT 'open',  -- open | actioned | dismissed
    handled_by   INTEGER,
    case_number  INTEGER,
    resolution   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    handled_at   TEXT,
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS idx_member_reports ON member_reports(guild_id, status);

-- ---------------------------------------------------------------- automation
CREATE TABLE IF NOT EXISTS automations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT,
    trigger      TEXT NOT NULL,
    conditions   TEXT NOT NULL DEFAULT '[]',
    actions      TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    run_count    INTEGER NOT NULL DEFAULT 0,
    last_run_at  TEXT,
    last_error   TEXT,
    last_error_at TEXT,
    created_by   INTEGER,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_automations_trigger ON automations(guild_id, trigger, enabled);

CREATE TABLE IF NOT EXISTS automation_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    automation_id INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    event_key     TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    detail        TEXT,
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (automation_id, event_key)   -- the same event never runs the same automation twice
);
CREATE INDEX IF NOT EXISTS idx_automation_runs ON automation_runs(guild_id, at);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    name              TEXT NOT NULL,
    channel_id        INTEGER NOT NULL,
    content           TEXT,
    template_id       INTEGER,
    interval_minutes  INTEGER,          -- NULL = send once
    next_run_at       TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_run_at       TEXT,
    last_error        TEXT,
    created_by        INTEGER,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_messages(enabled, next_run_at);

-- ---------------------------------------------------------------- notifications
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',  -- info | success | warning | critical
    title       TEXT NOT NULL,
    body        TEXT,
    link        TEXT,                          -- dashboard route, e.g. #/applications/12
    permission  TEXT,                          -- only users with this permission see it
    dedupe_key  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications ON notifications(guild_id, id);
CREATE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications(guild_id, dedupe_key);

CREATE TABLE IF NOT EXISTS notification_reads (
    notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL,
    read_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (notification_id, user_id)
);

-- ---------------------------------------------------------------- embeds, usage, dashboard
CREATE TABLE IF NOT EXISTS embed_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    data        TEXT NOT NULL,      -- JSON embed definition
    created_by  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS command_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER,
    user_id   INTEGER,
    command   TEXT NOT NULL,
    ok        INTEGER NOT NULL DEFAULT 1,
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_command_usage ON command_usage(guild_id, at);

CREATE TABLE IF NOT EXISTS dashboard_prefs (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    prefs      TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS web_sessions (
    sid_hash    TEXT PRIMARY KEY,   -- SHA-256 of the cookie value; the cookie itself is never stored
    user_id     INTEGER NOT NULL,
    username    TEXT,
    avatar_url  TEXT,
    csrf        TEXT NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);

-- ---------------------------------------------------------------- dispatch additions
ALTER TABLE incidents ADD COLUMN dispatcher_id INTEGER;
ALTER TABLE incidents ADD COLUMN workflow_status TEXT;
