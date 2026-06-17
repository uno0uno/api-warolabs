-- 089_minimum_consumption_config.sql
-- warocol.com#1368 — tenant-level minimum consumption / cover config.
-- Config only: session snapshots, deposits, close blocking, accounting and
-- reporting are handled by later epic batches.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS minimum_consumption_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS minimum_consumption_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS minimum_consumption_restrictive BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_minimum_consumption_amount_check;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_minimum_consumption_amount_check
    CHECK (minimum_consumption_amount >= 0);

COMMENT ON COLUMN tenant_public_profiles.minimum_consumption_enabled IS
    'When true, table sessions may use tenant minimum consumption / cover rules. Config only in #1368.';

COMMENT ON COLUMN tenant_public_profiles.minimum_consumption_amount IS
    'Tenant minimum consumption / cover amount in COP for table sessions. Config only in #1368.';

COMMENT ON COLUMN tenant_public_profiles.minimum_consumption_restrictive IS
    'When true, later close-session batches may block closing below the configured minimum. No enforcement in #1368.';
