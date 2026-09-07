-- Migration 122: Replace uq_period_tenant_active with shift-aware variant
-- Issue: https://github.com/uno0uno/api-warolabs/issues/898
--
-- Waro_Colombia runs two shifts per day (cafeteria 07:00-17:00 + restaurante 17:10-22:30).
-- The existing partial unique index on (tenant_id, period_start, period_end) blocks the
-- second shift's INSERT with `duplicate key value violates unique constraint
-- "uq_period_tenant_active"`. The overlap check by time passes, but the index doesn't
-- know about shift_template_id.
--
-- Replace the index with one that includes shift_template_id. Use COALESCE to map NULL
-- (legacy day-only / custom-window cierres) to a sentinel UUID so each tenant can still
-- have at most one NULL-template cierre per day.
--
-- Backfill check (run before applying in production):
--   SELECT tenant_id, period_start, period_end,
--          COALESCE(shift_template_id, '00000000-0000-0000-0000-000000000000'::uuid),
--          COUNT(*)
--   FROM accounting_period WHERE deleted_at IS NULL
--   GROUP BY 1,2,3,4 HAVING COUNT(*) > 1;
-- Expected: 0 rows (validated on 2026-08-24 against postresWaroLabs).

DROP INDEX IF EXISTS uq_period_tenant_active;

CREATE UNIQUE INDEX IF NOT EXISTS uq_period_tenant_shift_active
    ON accounting_period (
        tenant_id,
        period_start,
        period_end,
        COALESCE(shift_template_id, '00000000-0000-0000-0000-000000000000'::uuid)
    )
    WHERE deleted_at IS NULL;

COMMENT ON INDEX uq_period_tenant_shift_active IS
    'Unique active (tenant, day, shift_template) accounting periods (api-warolabs#898). '
    'COALESCE on shift_template_id preserves uniqueness for legacy NULL-template cierres.';
