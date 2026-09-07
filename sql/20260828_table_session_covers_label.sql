-- warocol.com#2469 — sale snapshot: covers vs catalog table capacity, custom session label.

ALTER TABLE table_sessions
    ADD COLUMN IF NOT EXISTS covers INTEGER,
    ADD COLUMN IF NOT EXISTS capacity_snapshot INTEGER,
    ADD COLUMN IF NOT EXISTS custom_label TEXT;

ALTER TABLE table_sessions
    DROP CONSTRAINT IF EXISTS table_sessions_covers_check;
ALTER TABLE table_sessions
    ADD CONSTRAINT table_sessions_covers_check
    CHECK (covers IS NULL OR covers >= 1);

ALTER TABLE table_sessions
    DROP CONSTRAINT IF EXISTS table_sessions_capacity_snapshot_check;
ALTER TABLE table_sessions
    ADD CONSTRAINT table_sessions_capacity_snapshot_check
    CHECK (capacity_snapshot IS NULL OR capacity_snapshot >= 1);
