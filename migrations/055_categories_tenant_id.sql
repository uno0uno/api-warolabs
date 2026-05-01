-- Issue #458: Categories — add per-tenant scoping
--
-- Existing rows keep tenant_id = NULL (they remain global, visible to every tenant).
-- New categories created via POST /menu/categories are written with tenant_id = current_tenant.
-- A functional unique index on (LOWER(name), COALESCE(tenant_id, 0)) prevents
-- case-insensitive duplicates within the global pool AND within each tenant's own pool.

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS tenant_id uuid NULL REFERENCES tenants(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_categories_tenant
    ON categories (tenant_id)
    WHERE tenant_id IS NOT NULL;

-- COALESCE to a sentinel zero-uuid lets a single index enforce uniqueness for both
-- (NULL tenant) "global" rows and (set tenant) per-tenant rows. Without the
-- COALESCE, NULL would never collide with NULL and "Bebidas" / "BEBIDAS" globals
-- could be created in parallel.
CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_tenant_unique
    ON categories (LOWER(name), COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid));
