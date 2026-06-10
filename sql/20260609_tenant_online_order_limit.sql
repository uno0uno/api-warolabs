-- warocol.com#1292: configurable online order maximum from /negocio
ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS online_order_max_amount NUMERIC(12, 2);

COMMENT ON COLUMN tenant_public_profiles.online_order_max_amount IS
    'Tenant-configured maximum COP amount for online/delivery customer validation. NULL keeps tier defaults; 0 disables the amount limit.';
