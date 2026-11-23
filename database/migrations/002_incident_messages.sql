-- Discord messages that mirror an incident (dispatch-log cards, supervisor alerts) so they can be edited later.
CREATE TABLE IF NOT EXISTS incident_messages (
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- log_card | supervisor_alert
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    marker      TEXT,
    PRIMARY KEY (incident_id, kind)
);
