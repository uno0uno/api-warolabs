-- uno0uno/warocol.com#2237 — per-tenant order for online menu products within a category.
-- Additive only: mirror tenant_online_menu_category_orders.

CREATE TABLE IF NOT EXISTS tenant_online_menu_product_orders (
    tenant_id UUID NOT NULL,
    product_id UUID NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, product_id)
);

COMMENT ON TABLE tenant_online_menu_product_orders IS
    'Per-tenant display order for products on public online menus (domicilios + mesa QR), within category order.';

CREATE INDEX IF NOT EXISTS idx_tompo_tenant_display_order
    ON tenant_online_menu_product_orders (tenant_id, display_order);
