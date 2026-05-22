-- warocol.com#805: self-service venta libre toggle (Operaciones → Personalizar)
ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS open_sale_enabled BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.open_sale_enabled IS
    'When true, POS shows Venta libre and shell open_priced product is active for the tenant.';
