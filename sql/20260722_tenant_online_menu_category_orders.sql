-- uno0uno/api-warolabs#690 — per-tenant order for online menu category chips.
-- Additive only: globals stay on categories; order lives in junction rows.

CREATE TABLE IF NOT EXISTS tenant_online_menu_category_orders (
    tenant_id UUID NOT NULL,
    category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, category_id)
);

COMMENT ON TABLE tenant_online_menu_category_orders IS
    'Per-tenant display order for categories shown on the public online menu (domicilios).';

CREATE INDEX IF NOT EXISTS idx_tomco_tenant_display_order
    ON tenant_online_menu_category_orders (tenant_id, display_order);
