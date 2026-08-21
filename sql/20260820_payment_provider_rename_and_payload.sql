-- uno0uno/api-warolabs#894 — provider-agnostic rename + payload persistence.
-- Additive: rename tables, add new columns, backfill provider='wompi' for existing rows.
-- Postgres 11+ ADD COLUMN with constant DEFAULT is an instant metadata-only operation.
-- The (tenant_id, provider_tx_id) unique index is auto-renamed by Postgres when the table is renamed.

BEGIN;

ALTER TABLE IF EXISTS tenant_wompi_merchants
    RENAME TO tenant_payment_providers;

ALTER TABLE IF EXISTS tenant_wompi_collection_sessions
    RENAME TO tenant_collection_sessions;

ALTER TABLE IF EXISTS tenant_collection_sessions
    ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'wompi';

ALTER TABLE IF EXISTS tenant_collection_sessions
    ADD COLUMN IF NOT EXISTS provider_payload JSONB;

ALTER TABLE IF EXISTS tenant_collection_sessions
    ADD COLUMN IF NOT EXISTS provider_payment_method_type TEXT;

ALTER TABLE IF EXISTS tenant_collection_sessions
    ADD COLUMN IF NOT EXISTS customer_email TEXT;

ALTER TABLE IF EXISTS tenant_collection_sessions
    ADD COLUMN IF NOT EXISTS currency TEXT;

ALTER TABLE IF EXISTS tenant_collection_sessions
    ADD COLUMN IF NOT EXISTS environment TEXT;

-- Backfill is implicit via DEFAULT 'wompi' on the new column for existing rows.
-- This UPDATE is a no-op safety net (idempotent) and keeps documentation intent explicit.
UPDATE tenant_collection_sessions
SET provider = 'wompi'
WHERE provider IS NULL OR provider = '';

COMMIT;
