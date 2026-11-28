-- Hive Dispatch | ERLC - rename from Pulse Dispatch.
-- Saved server settings, embed templates, automations and scheduled messages may still contain the old name
-- (for example a footer saved from the old default). Rewrite it once so every embed says Hive Dispatch.
UPDATE guild_config      SET value   = REPLACE(REPLACE(value,   'Pulse Dispatch', 'Hive Dispatch'), 'PULSE DISPATCH', 'HIVE DISPATCH') WHERE value   LIKE '%Pulse Dispatch%';
UPDATE embed_templates   SET data    = REPLACE(REPLACE(data,    'Pulse Dispatch', 'Hive Dispatch'), 'PULSE DISPATCH', 'HIVE DISPATCH') WHERE data    LIKE '%Pulse Dispatch%';
UPDATE automations       SET actions = REPLACE(REPLACE(actions, 'Pulse Dispatch', 'Hive Dispatch'), 'PULSE DISPATCH', 'HIVE DISPATCH') WHERE actions LIKE '%Pulse Dispatch%';
UPDATE scheduled_messages SET content = REPLACE(REPLACE(content, 'Pulse Dispatch', 'Hive Dispatch'), 'PULSE DISPATCH', 'HIVE DISPATCH') WHERE content LIKE '%Pulse Dispatch%';
