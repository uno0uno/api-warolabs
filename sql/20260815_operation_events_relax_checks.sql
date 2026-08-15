-- Bitácora across modules (warocol.com#2323).
-- Reuse `domain` as the module key. Drop closed CHECKs so later batches
-- only expand the Python allowlist. Channel is nullable for non-POS rows.

ALTER TABLE tenant_operation_events
    DROP CONSTRAINT IF EXISTS tenant_operation_events_domain_check;

ALTER TABLE tenant_operation_events
    DROP CONSTRAINT IF EXISTS tenant_operation_events_channel_check;

ALTER TABLE tenant_operation_events
    DROP CONSTRAINT IF EXISTS tenant_operation_events_action_check;

ALTER TABLE tenant_operation_events
    ALTER COLUMN channel DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tenant_operation_events_tenant_domain
    ON tenant_operation_events (tenant_id, domain, created_at DESC);

COMMENT ON TABLE tenant_operation_events IS
    'Append-only operation audit log for Bitácora. domain is the module key.';
