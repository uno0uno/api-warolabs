-- warocol.com#795: open-priced (venta libre) shell product flag
ALTER TABLE product
    ADD COLUMN IF NOT EXISTS open_priced BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN product.open_priced IS
    'When true, POS may send a custom unit_price for this product (venta libre). At most one per tenant.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_one_open_priced_per_tenant
    ON product (tenant_id)
    WHERE open_priced = true;
