-- warocol.com#1369 — snapshot tenant minimum consumption config on table sessions.

ALTER TABLE table_sessions
    ADD COLUMN IF NOT EXISTS minimum_consumption_enabled_snapshot BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS minimum_consumption_amount_snapshot NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS minimum_consumption_restrictive_snapshot BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE table_sessions
    DROP CONSTRAINT IF EXISTS table_sessions_minimum_consumption_amount_snapshot_check;

ALTER TABLE table_sessions
    ADD CONSTRAINT table_sessions_minimum_consumption_amount_snapshot_check
    CHECK (minimum_consumption_amount_snapshot >= 0);

COMMENT ON COLUMN table_sessions.minimum_consumption_enabled_snapshot IS
    'Snapshot of tenant minimum consumption / cover enabled flag when the session opened.';

COMMENT ON COLUMN table_sessions.minimum_consumption_amount_snapshot IS
    'Snapshot of tenant minimum consumption / cover amount in COP when the session opened.';

COMMENT ON COLUMN table_sessions.minimum_consumption_restrictive_snapshot IS
    'Snapshot of tenant restrictive close flag when the session opened. Enforcement is handled in later batches.';
