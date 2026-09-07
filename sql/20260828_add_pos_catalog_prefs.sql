-- warocol.com#2495 — tenant POS catalog presentation defaults (ADD-only)
ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS pos_catalog_layout_default text NOT NULL DEFAULT 'grid',
    ADD COLUMN IF NOT EXISTS pos_show_product_image boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS pos_show_search boolean NOT NULL DEFAULT true;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tenant_public_profiles_pos_catalog_layout_default_check'
    ) THEN
        ALTER TABLE tenant_public_profiles
            ADD CONSTRAINT tenant_public_profiles_pos_catalog_layout_default_check
            CHECK (pos_catalog_layout_default IN ('grid', 'list'));
    END IF;
END $$;

COMMENT ON COLUMN tenant_public_profiles.pos_catalog_layout_default IS
    'POS catalog default layout: grid | list (warocol.com#2495). Per-user override is separate.';
COMMENT ON COLUMN tenant_public_profiles.pos_show_product_image IS
    'When true, POS product cards/rows show product images (warocol.com#2495).';
COMMENT ON COLUMN tenant_public_profiles.pos_show_search IS
    'When true, POS catalog search bar is visible (warocol.com#2495).';
