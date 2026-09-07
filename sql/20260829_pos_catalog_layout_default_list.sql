-- Set POS catalog tenant default to list/table for all tenants + future rows.
-- Non-destructive: UPDATE + ALTER DEFAULT only (no DROP).
-- Users keep personal override in profile.pos_catalog_layout_override if set.

UPDATE tenant_public_profiles
SET pos_catalog_layout_default = 'list'
WHERE pos_catalog_layout_default IS DISTINCT FROM 'list';

ALTER TABLE tenant_public_profiles
    ALTER COLUMN pos_catalog_layout_default SET DEFAULT 'list';

COMMENT ON COLUMN tenant_public_profiles.pos_catalog_layout_default IS
    'POS catalog default layout: grid | list. Default list; per-user override is separate.';
