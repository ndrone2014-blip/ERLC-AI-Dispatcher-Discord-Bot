-- v5.0 Platform Overhaul. Adds tables and columns only: nothing existing is dropped or renamed.

-- ---------------------------------------------------------------- ER:LC players seen in-game
CREATE TABLE IF NOT EXISTS erlc_players (
    guild_id         INTEGER NOT NULL,
    roblox_id        INTEGER NOT NULL,
    username         TEXT NOT NULL,
    first_seen       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_join        TEXT,
    last_leave       TEXT,
    sessions         INTEGER NOT NULL DEFAULT 0,
    online           INTEGER NOT NULL DEFAULT 0,
    team             TEXT,
    permission       TEXT,
    callsign         TEXT,
    PRIMARY KEY (guild_id, roblox_id)
);
CREATE INDEX IF NOT EXISTS idx_erlc_players_name ON erlc_players(guild_id, username COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_erlc_players_online ON erlc_players(guild_id, online);

-- Joins and leaves detected by comparing the live player list between polls.
CREATE TABLE IF NOT EXISTS erlc_player_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    roblox_id  INTEGER NOT NULL,
    username   TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- join | leave
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    team       TEXT
);
CREATE INDEX IF NOT EXISTS idx_erlc_player_events ON erlc_player_events(guild_id, at);
CREATE INDEX IF NOT EXISTS idx_erlc_player_events_player ON erlc_player_events(guild_id, roblox_id, at);

-- ---------------------------------------------------------------- in-game (ER:LC) moderation
CREATE TABLE IF NOT EXISTS erlc_mod_records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    number           INTEGER NOT NULL,
    action           TEXT NOT NULL,      -- warning | kick | ban | tempban | unban | note
    roblox_id        INTEGER,
    roblox_username  TEXT NOT NULL,
    moderator_id     INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    evidence         TEXT,
    duration_s       INTEGER,
    expires_at       TEXT,
    status           TEXT NOT NULL DEFAULT 'active',   -- active | revoked | expired
    in_game          TEXT NOT NULL DEFAULT 'not_requested', -- not_requested | sent | failed
    in_game_detail   TEXT,
    source           TEXT NOT NULL DEFAULT 'dashboard', -- dashboard | discord
    revoked_by       INTEGER,
    revoked_at       TEXT,
    revoke_reason    TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS idx_erlc_mod_player ON erlc_mod_records(guild_id, roblox_id);
CREATE INDEX IF NOT EXISTS idx_erlc_mod_name ON erlc_mod_records(guild_id, roblox_username COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_erlc_mod_created ON erlc_mod_records(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_erlc_mod_moderator ON erlc_mod_records(guild_id, moderator_id);

CREATE TABLE IF NOT EXISTS player_notes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    roblox_id        INTEGER,
    roblox_username  TEXT NOT NULL,
    author_id        INTEGER NOT NULL,
    note             TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_player_notes ON player_notes(guild_id, roblox_id);

-- ---------------------------------------------------------------- BOLOs (informational, never a ban)
ALTER TABLE bolos ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium';   -- low | medium | high | critical
ALTER TABLE bolos ADD COLUMN roblox_id INTEGER;
ALTER TABLE bolos ADD COLUMN roblox_username TEXT;
ALTER TABLE bolos ADD COLUMN close_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_bolos_status ON bolos(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_bolos_player ON bolos(guild_id, roblox_id);

CREATE TABLE IF NOT EXISTS bolo_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    bolo_id     INTEGER NOT NULL REFERENCES bolos(id) ON DELETE CASCADE,
    author_id   INTEGER NOT NULL,
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_bolo_notes ON bolo_notes(bolo_id);

-- ---------------------------------------------------------------- activity feed
-- Every logbook entry lands here whether or not its Discord log category is switched on.
CREATE TABLE IF NOT EXISTS activity_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    actor_id    INTEGER,
    link        TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_guild ON activity_events(guild_id, id);
CREATE INDEX IF NOT EXISTS idx_activity_cat ON activity_events(guild_id, category, id);
