-- warocol.com#2574 — hide catalog products that cannot be made from recipe stock (ADD-only)
ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS hide_products_without_stock boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.hide_products_without_stock IS
    'When true, POS/manual (and later online/QR) catalogs hide products with a recipe that cannot make qty>=1 from tenant_inventory. Products without recipes stay visible. Default false (opt-in). warocol.com#2574';
