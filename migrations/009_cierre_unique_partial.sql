-- Migration 009: Replace uq_period_tenant with a partial unique index
-- The old constraint blocks re-creating a cierre for the same period after a soft delete.
-- A partial index (WHERE deleted_at IS NULL) enforces uniqueness only among active records.

ALTER TABLE accounting_period
    DROP CONSTRAINT IF EXISTS uq_period_tenant;

CREATE UNIQUE INDEX IF NOT EXISTS uq_period_tenant_active
    ON accounting_period (tenant_id, period_start, period_end)
    WHERE deleted_at IS NULL;
