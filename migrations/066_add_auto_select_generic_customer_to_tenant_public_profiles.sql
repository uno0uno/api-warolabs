-- 066_add_auto_select_generic_customer_to_tenant_public_profiles.sql
-- Issue #529 — feat(pos-checkout): auto-select Genérico customer at checkout open
--
-- Adds a per-tenant boolean flag that, when true, makes /pos/checkout
-- pre-select the Genérico customer (phone_number='0000000000') in counter
-- and bar modes, eliminating the modal step on every fast counter sale.
--
-- Default false to preserve current behavior for existing tenants.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS auto_select_generic_enabled BOOLEAN NOT NULL DEFAULT false;
