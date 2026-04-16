-- Migration 028: GL account mapping for payment methods and groups
-- Issue #106 — Auto-posting compras-directas → GL
--
-- Two-level mapping (same pattern as Siigo Colombia, Xero, Lightspeed):
--   1. payment_methods.gl_account_code       — per-method override (e.g. Nequi → 111005-01)
--   2. payment_method_groups.gl_account_code — group default (e.g. digital → 1110)
--
-- Resolution order: individual method → group default → hardcoded fallback
--
-- PUC Colombia defaults:
--   cash    → 1105  Caja general
--   digital → 1110  Bancos (Nequi, Daviplata, PSE are banking products, not caja)
--   card    → 1110  Bancos (datáfono settlement, simplified — no clearing account yet)
--   credit  → 1305  Clientes (fiado — accounts receivable)

-- Group-level default GL account
ALTER TABLE payment_method_groups
    ADD COLUMN IF NOT EXISTS gl_account_code VARCHAR(20) DEFAULT NULL;

-- Individual method-level override (optional — inherits group if null)
ALTER TABLE payment_methods
    ADD COLUMN IF NOT EXISTS gl_account_code VARCHAR(20) DEFAULT NULL;

-- Seed defaults for the four global groups (tenant_id IS NULL)
UPDATE payment_method_groups SET gl_account_code = '1105' WHERE slug = 'cash'    AND tenant_id IS NULL;
UPDATE payment_method_groups SET gl_account_code = '1110' WHERE slug = 'digital' AND tenant_id IS NULL;
UPDATE payment_method_groups SET gl_account_code = '1110' WHERE slug = 'card'    AND tenant_id IS NULL;
UPDATE payment_method_groups SET gl_account_code = '1305' WHERE slug = 'credit'  AND tenant_id IS NULL;

-- Tenant-created groups inherit NULL gl_account_code — must be configured by tenant.
