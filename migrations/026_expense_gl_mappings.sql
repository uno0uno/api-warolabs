-- Migration 026: Expense category → GL account mapping table
-- Issue #377 — Auto-posting from gastos module to GL

CREATE TABLE IF NOT EXISTS expense_category_gl_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    category_code VARCHAR(50) NOT NULL,
    debit_account_code VARCHAR(20) NOT NULL,          -- PUC expense account (Clase 5 or 6)
    credit_cash_account_code VARCHAR(20) NOT NULL,    -- 1105 Caja general (payment_method='cash')
    credit_default_account_code VARCHAR(20) NOT NULL, -- 2205/2335 for digital/null payment
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, category_code)
);

-- Seed default mappings for all existing tenants
-- New tenants receive these defaults via seed_tenant_accounts() (to be extended)
DO $$
DECLARE
    t_id UUID;
BEGIN
    FOR t_id IN SELECT id FROM tenants LOOP
        INSERT INTO expense_category_gl_mappings
            (tenant_id, category_code, debit_account_code, credit_cash_account_code, credit_default_account_code)
        VALUES
            (t_id, 'SUPPLIES',     '6135', '1105', '2205'),
            (t_id, 'PAYROLL',      '5105', '1105', '2335'),
            (t_id, 'RENT',         '5140', '1105', '2205'),
            (t_id, 'UTILITIES',    '5135', '1105', '2335'),
            (t_id, 'MAINTENANCE',  '5145', '1105', '2205'),
            (t_id, 'MARKETING',    '5195', '1105', '2335'),
            (t_id, 'PROFESSIONAL', '5195', '1105', '2335'),
            (t_id, 'INSURANCE',    '5195', '1105', '2335'),
            (t_id, 'CAPITAL',      '5195', '1105', '2335'),
            (t_id, 'CONTINGENCY',  '5195', '1105', '2335')
        ON CONFLICT (tenant_id, category_code) DO NOTHING;
    END LOOP;
END;
$$;
