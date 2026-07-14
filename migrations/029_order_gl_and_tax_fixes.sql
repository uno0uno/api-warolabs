-- Migration 029: Fix GL infrastructure for per-order accounting
-- Issue #389 — https://github.com/uno0uno/warocol.com/issues/389
--
-- Changes:
--   1. Extend source_module CHECK: add 'orden' and 'cartera'
--   2. Rename account 2368 to correct PUC name (ICA retenido, not INC)
--   3. Add group 24 "Impuestos, gravámenes y tasas" (FK parent for 2408, 2495)
--   4. Add 2495 "Impoconsumo por pagar (INC)" — correct PUC account for INC
--   5. Add 2408 "IVA por pagar" — for franchise restaurants
--   6. Re-seed all tenants with new accounts
--   7. Fix tenant_tax_config: INC → 2495, update column defaults
--   8. Add tax mode columns: inc_included_in_price, iva_included_in_price
--   9. Index on source_module='orden' for fast per-order GL lookups

-- ─────────────────────────────────────────────
-- 1. Extend source_module CHECK constraint
--    Adds 'orden' (individual sale entries) and 'cartera' (future)
--    Safe: existing rows ('gastos','ventas','nomina',etc.) remain valid
-- ─────────────────────────────────────────────
ALTER TABLE tenant_journal_entries
    DROP CONSTRAINT IF EXISTS tenant_journal_entries_source_module_check;

ALTER TABLE tenant_journal_entries
    ADD CONSTRAINT tenant_journal_entries_source_module_check
    CHECK (source_module IN (
        'gastos',
        'ventas',       -- cierre / arqueo consolidated entries
        'orden',        -- individual POS or domicilio sale entries ← NEW
        'nomina',
        'inventario',
        'cartera',      -- future: cartera payment GL entries ← NEW
        'arqueo',
        'manual',
        'system'
    ));

-- ─────────────────────────────────────────────
-- 2. Rename account 2368 to correct PUC Decreto 2650/1993 name
--    2368 = "Impuesto de industria y comercio retenido" (ICA retenido)
--    It was mislabeled as "INC por pagar" in the original seed.
--    INC (Ley 1607/2012) has no own code in Decreto 2650 — uses 2495 instead.
--    Must update both account_templates (source) and tenant_accounts (denormalized copy).
-- ─────────────────────────────────────────────
UPDATE account_templates
    SET standard_name = 'Impuesto de industria y comercio retenido'
    WHERE localization_id = 'WARO_CO_PUC_V1'
      AND code = '2368';

UPDATE tenant_accounts
    SET name = 'Impuesto de industria y comercio retenido'
    WHERE code = '2368';

-- ─────────────────────────────────────────────
-- 3. Add group 24 "Impuestos, gravámenes y tasas"
--    Required FK parent (parent_code = '24') before inserting 2408 and 2495.
--    Group 24 did not exist in the original PUC seed.
-- ─────────────────────────────────────────────
INSERT INTO account_templates (
    code, standard_name, account_class, account_type,
    normal_balance, level, parent_code, is_detail, niif_group, is_active
) VALUES (
    '24', 'Impuestos, gravámenes y tasas',
    '2', 'liability', 'credit', 2, '2', false, 'grupo2', true
) ON CONFLICT (localization_id, code) DO NOTHING;

-- ─────────────────────────────────────────────
-- 4. Add 2495 "Impoconsumo por pagar (INC)"
--    Correct PUC account for Impuesto Nacional al Consumo (Art. 512-1 ET).
--    INC was created by Ley 1607/2012 — 19 years after Decreto 2650/1993.
--    Standard accounting practice: use group 24/2495 "Otros" with auxiliary sub-account.
--    Rate: 8% on restaurant/bar service. Declared bimonthly via DIAN Form. 310.
-- ─────────────────────────────────────────────
INSERT INTO account_templates (
    code, standard_name, account_class, account_type,
    normal_balance, level, parent_code, is_detail, niif_group, is_active
) VALUES (
    '2495', 'Impoconsumo por pagar (INC)',
    '2', 'liability', 'credit', 4, '24', true, 'grupo2', true
) ON CONFLICT (localization_id, code) DO NOTHING;

-- ─────────────────────────────────────────────
-- 5. Add 2408 "IVA por pagar"
--    For restaurants operating under franchise (IVA 19% instead of INC).
--    Also used for takeout liquor sales (IVA 5%).
--    Declared via DIAN Form. 300.
-- ─────────────────────────────────────────────
INSERT INTO account_templates (
    code, standard_name, account_class, account_type,
    normal_balance, level, parent_code, is_detail, niif_group, is_active
) VALUES (
    '2408', 'Impuesto sobre las ventas por pagar (IVA)',
    '2', 'liability', 'credit', 4, '24', true, 'grupo2', true
) ON CONFLICT (localization_id, code) DO NOTHING;

-- ─────────────────────────────────────────────
-- 6. Re-seed all existing tenants
--    Adds group 24, accounts 2408 and 2495 to every chart of accounts.
--    ON CONFLICT DO NOTHING — safe to run, skips already-seeded accounts.
--    Parent link (parent_id UUID) resolved by seed_tenant_accounts step 2.
-- ─────────────────────────────────────────────
DO $$
DECLARE
    t_id UUID;
BEGIN
    FOR t_id IN SELECT id FROM tenants LOOP
        PERFORM seed_tenant_accounts(t_id);
    END LOOP;
END $$;

-- ─────────────────────────────────────────────
-- 7. Fix tenant_tax_config: correct INC account code
--    All 11 tenants had inc_gl_account_code = '2408' (wrong: account didn't exist).
--    INC must point to 2495; IVA stays at 2408 (now seeded above).
-- ─────────────────────────────────────────────
UPDATE tenant_tax_config
    SET inc_gl_account_code = '2495'
    WHERE inc_gl_account_code IN ('2368', '2408');

ALTER TABLE tenant_tax_config
    ALTER COLUMN inc_gl_account_code SET DEFAULT '2495',
    ALTER COLUMN iva_gl_account_code SET DEFAULT '2408';

-- ─────────────────────────────────────────────
-- 8. Add tax mode columns
--    inc_included_in_price: true  → price already contains INC (extract formula)
--                           false → add INC on top of base price (additive formula)
--    iva_included_in_price: true  → price already contains IVA (extract formula)
--                           false → add IVA on top of base price (default for franchises)
--    PG 11+: ADD COLUMN with constant default is metadata-only (instant, no table rewrite)
-- ─────────────────────────────────────────────
ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS inc_included_in_price BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS iva_included_in_price BOOLEAN NOT NULL DEFAULT false;

-- ─────────────────────────────────────────────
-- 9. Index for per-order GL lookups
--    Used for: idempotency checks, audit trail, refund lookups
--    Non-CONCURRENTLY: safe inside this transaction for a small table
-- ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_je_orden
    ON tenant_journal_entries(tenant_id, source_id)
    WHERE source_module = 'orden';
