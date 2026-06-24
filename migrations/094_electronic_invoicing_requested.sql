-- Migration 094: customer-controlled electronic invoicing request
--
-- Separates the customer's intent to use electronic invoicing from WARO's
-- internal Matias/DIAN enablement switch on tenants.electronic_invoicing_enabled.

ALTER TABLE tenant_fiscal_data
    ADD COLUMN IF NOT EXISTS electronic_invoicing_requested BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_fiscal_data.electronic_invoicing_requested IS
    'Customer-controlled request/intent to use electronic invoicing; does not enable DIAN/Matias emission by itself.';
