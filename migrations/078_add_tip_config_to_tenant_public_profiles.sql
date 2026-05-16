-- 078_add_tip_config_to_tenant_public_profiles.sql
-- Issue warocol.com#635 — per-tenant tipping configuration.
--
-- Adds three columns that control tip capture at checkout:
--   tip_enabled              — master toggle (default false: hidden)
--   tip_default_percentages  — suggested presets (default {10})
--   tip_preselect_index      — index into the array to pre-select, NULL = none
--
-- Defaults preserve current behaviour: tipping is hidden until the tenant
-- explicitly opts in. Phase 1 ships the direct-attribution model — the tip
-- is recorded against orders.served_by_member_id (#575). Pooled distribution
-- (points × hours, role weights, etc.) is intentionally out of scope.
--
-- NUMERIC(5,2)[] allows half-percent presets (e.g. 12.5%) without a future
-- migration. tip_preselect_index defaults to NULL so the checkout never
-- pre-selects a tip — required by Ley 1935/2018 voluntariness.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS tip_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS tip_default_percentages NUMERIC(5,2)[] NOT NULL DEFAULT ARRAY[10]::NUMERIC(5,2)[],
    ADD COLUMN IF NOT EXISTS tip_preselect_index INT NULL;

COMMENT ON COLUMN tenant_public_profiles.tip_enabled IS
    'Master tipping toggle (warocol.com#635). When true, surfaces the tip '
    'selector at POS/online checkout and the /ventas/propinas history view. '
    'Default false preserves current behaviour.';

COMMENT ON COLUMN tenant_public_profiles.tip_default_percentages IS
    'Suggested tip presets shown as chips at checkout (warocol.com#635). '
    'Resolved on subtotal (pre-tax). App-level validation enforces max 5 '
    'entries, each between 0 and 100.';

COMMENT ON COLUMN tenant_public_profiles.tip_preselect_index IS
    'Index into tip_default_percentages to pre-select at checkout. NULL '
    'means nothing is pre-selected (recommended — Ley 1935/2018 voluntariness).';
