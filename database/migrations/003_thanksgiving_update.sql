-- Hive Dispatch | ERLC - Thanksgiving Update
-- Persistent dispatch log (always available, even without a log channel) + performance indexes.
CREATE TABLE IF NOT EXISTS dispatch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    detail      TEXT,
    incident_id INTEGER,
    actor_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dispatch_log_guild ON dispatch_log(guild_id, id);
CREATE INDEX IF NOT EXISTS idx_dispatch_log_cat ON dispatch_log(guild_id, category, id);

CREATE INDEX IF NOT EXISTS idx_incidents_guild_created ON incidents(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assignments_unit ON incident_assignments(unit_id, cleared_at);
CREATE INDEX IF NOT EXISTS idx_assignments_guild ON incident_assignments(guild_id, assigned_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_guild ON ai_usage(guild_id, at);
CREATE INDEX IF NOT EXISTS idx_unit_status_history ON unit_status_history(guild_id, at);
CREATE INDEX IF NOT EXISTS idx_announcements_dedupe ON announcements(guild_id, dedupe_key);
