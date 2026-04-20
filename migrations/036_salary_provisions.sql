-- Migration 036: Automatic social benefit provisions on salary payment
-- Issue #404 — prestaciones sociales (prima, cesantías, int. cesantías, vacaciones)
--
-- PART A: Create salary_provisions table
-- PART B: Add 4 expense account templates (5106–5109) for provisions
-- PART C: Back-fill tenant_accounts for all existing tenants
--
-- Safety: ADD-only — no DROP, no ALTER of existing rows.
-- All inserts use ON CONFLICT DO NOTHING — safe to re-run.

-- ─────────────────────────────────────────────
-- PART A: Create salary_provisions table
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS salary_provisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    payment_id      UUID NOT NULL REFERENCES salary_payments(id) ON DELETE CASCADE,
    period_month    VARCHAR(7) NOT NULL,
    employment_type VARCHAR(20) NOT NULL,
    gross_salary    NUMERIC(12,2) NOT NULL,
    prima           NUMERIC(12,2) NOT NULL,
    cesantias       NUMERIC(12,2) NOT NULL,
    int_cesantias   NUMERIC(12,2) NOT NULL,
    vacaciones      NUMERIC(12,2) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_salary_provisions_payment
    ON salary_provisions(payment_id);

CREATE INDEX IF NOT EXISTS idx_salary_provisions_tenant_period
    ON salary_provisions(tenant_id, period_month);

-- ─────────────────────────────────────────────
-- PART B: Add expense account templates for provisions
-- Codes 5106–5109 — do not conflict with existing 5105, 5110, 5120, 5140, 5199
-- CR side accounts (2610, 2615, 2620, 2625) already seeded in migration 025
-- ─────────────────────────────────────────────
INSERT INTO account_templates (code, standard_name, account_class, account_type, normal_balance, level, parent_code, is_detail, niif_group, is_active)
VALUES
  ('5106', 'Gasto prima de servicios',         '5', 'expense', 'debit', 4, '51', true, 'grupo2', true),
  ('5107', 'Gasto cesantías',                  '5', 'expense', 'debit', 4, '51', true, 'grupo2', true),
  ('5108', 'Gasto intereses sobre cesantías',  '5', 'expense', 'debit', 4, '51', true, 'grupo2', true),
  ('5109', 'Gasto vacaciones',                 '5', 'expense', 'debit', 4, '51', true, 'grupo2', true)
ON CONFLICT (code) DO NOTHING;

-- ─────────────────────────────────────────────
-- PART C: Back-fill tenant_accounts for all tenants that have 5105 but not these codes
-- ─────────────────────────────────────────────
DO $$
DECLARE
    t_id         UUID;
    tmpl_5106    UUID;
    tmpl_5107    UUID;
    tmpl_5108    UUID;
    tmpl_5109    UUID;
    parent_acct_id UUID;
BEGIN
    -- Resolve template IDs
    SELECT id INTO tmpl_5106 FROM account_templates WHERE code = '5106';
    SELECT id INTO tmpl_5107 FROM account_templates WHERE code = '5107';
    SELECT id INTO tmpl_5108 FROM account_templates WHERE code = '5108';
    SELECT id INTO tmpl_5109 FROM account_templates WHERE code = '5109';

    IF tmpl_5106 IS NULL OR tmpl_5107 IS NULL OR tmpl_5108 IS NULL OR tmpl_5109 IS NULL THEN
        RAISE NOTICE 'One or more account_templates rows missing — skipping back-fill';
        RETURN;
    END IF;

    -- Iterate all tenants that already have 5105
    FOR t_id IN
        SELECT DISTINCT tenant_id
        FROM tenant_accounts
        WHERE code = '5105'
    LOOP
        -- Resolve parent account (code '51') for this tenant
        SELECT id INTO parent_acct_id
        FROM tenant_accounts
        WHERE tenant_id = t_id AND code = '51';

        -- 5106 — Gasto prima de servicios
        INSERT INTO tenant_accounts (
            tenant_id, template_id, code, name,
            account_class, account_type, normal_balance,
            level, parent_id, is_detail, is_system, is_active
        )
        VALUES (
            t_id, tmpl_5106, '5106', 'Gasto prima de servicios',
            '5', 'expense', 'debit',
            4, parent_acct_id, true, true, true
        )
        ON CONFLICT (tenant_id, code) DO NOTHING;

        -- 5107 — Gasto cesantías
        INSERT INTO tenant_accounts (
            tenant_id, template_id, code, name,
            account_class, account_type, normal_balance,
            level, parent_id, is_detail, is_system, is_active
        )
        VALUES (
            t_id, tmpl_5107, '5107', 'Gasto cesantías',
            '5', 'expense', 'debit',
            4, parent_acct_id, true, true, true
        )
        ON CONFLICT (tenant_id, code) DO NOTHING;

        -- 5108 — Gasto intereses sobre cesantías
        INSERT INTO tenant_accounts (
            tenant_id, template_id, code, name,
            account_class, account_type, normal_balance,
            level, parent_id, is_detail, is_system, is_active
        )
        VALUES (
            t_id, tmpl_5108, '5108', 'Gasto intereses sobre cesantías',
            '5', 'expense', 'debit',
            4, parent_acct_id, true, true, true
        )
        ON CONFLICT (tenant_id, code) DO NOTHING;

        -- 5109 — Gasto vacaciones
        INSERT INTO tenant_accounts (
            tenant_id, template_id, code, name,
            account_class, account_type, normal_balance,
            level, parent_id, is_detail, is_system, is_active
        )
        VALUES (
            t_id, tmpl_5109, '5109', 'Gasto vacaciones',
            '5', 'expense', 'debit',
            4, parent_acct_id, true, true, true
        )
        ON CONFLICT (tenant_id, code) DO NOTHING;

        RAISE NOTICE 'Provisioned 5106–5109 for tenant %', t_id;
    END LOOP;
END $$;
