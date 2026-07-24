-- Issue warocol.com#1776: hospitality Finanzas roles + expense GL maps.
-- ADD-only: new template accounts/role defaults, extend seed_tenant_accounts,
-- backfill existing hospitality tenants.

BEGIN;

INSERT INTO account_templates (
    localization_id, code, standard_name, account_class, account_type,
    normal_balance, level, parent_code, is_detail, niif_group, is_active
) VALUES
    ('WARO_HOSPITALITY_GLOBAL_V1', '4010', 'Other income',      '4', 'income',  'credit', 4, '40', true, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '5100', 'Bank fees expense', '5', 'expense', 'debit',  4, '50', true, NULL, true)
ON CONFLICT (localization_id, code) DO UPDATE SET
    standard_name = EXCLUDED.standard_name,
    account_class = EXCLUDED.account_class,
    account_type = EXCLUDED.account_type,
    normal_balance = EXCLUDED.normal_balance,
    level = EXCLUDED.level,
    parent_code = EXCLUDED.parent_code,
    is_detail = EXCLUDED.is_detail,
    niif_group = EXCLUDED.niif_group,
    is_active = EXCLUDED.is_active;

UPDATE account_templates child
SET parent_template_id = parent.id
FROM account_templates parent
WHERE parent.localization_id = child.localization_id
  AND parent.code = child.parent_code
  AND child.localization_id = 'WARO_HOSPITALITY_GLOBAL_V1'
  AND child.code IN ('4010', '5100')
  AND child.parent_template_id IS DISTINCT FROM parent.id;

WITH role_codes(role, code) AS (
    VALUES
        ('OTHER_INCOME', '4010'),
        ('BANK_FEES_EXPENSE', '5100')
)
INSERT INTO account_template_role_defaults (localization_id, role, account_template_id)
SELECT 'WARO_HOSPITALITY_GLOBAL_V1', role_codes.role, templates.id
FROM role_codes
JOIN account_templates templates
  ON templates.localization_id = 'WARO_HOSPITALITY_GLOBAL_V1'
 AND templates.code = role_codes.code
ON CONFLICT (localization_id, role) DO UPDATE
SET account_template_id = EXCLUDED.account_template_id;

CREATE OR REPLACE FUNCTION seed_tenant_accounts(
    p_tenant_id UUID,
    p_localization_id VARCHAR(50)
)
RETURNS VOID AS $$
DECLARE
    v_profile_localization VARCHAR(50);
BEGIN
    SELECT accounting_localization
    INTO v_profile_localization
    FROM tenant_financial_profiles
    WHERE tenant_id = p_tenant_id;

    IF v_profile_localization IS NULL THEN
        RAISE EXCEPTION 'Tenant % has no financial profile', p_tenant_id;
    END IF;

    IF v_profile_localization <> p_localization_id THEN
        RAISE EXCEPTION 'Localization % does not match tenant % profile %',
            p_localization_id, p_tenant_id, v_profile_localization;
    END IF;

    INSERT INTO tenant_accounts (
        tenant_id, template_id, code, name,
        account_class, account_type, normal_balance,
        level, is_detail, is_system, is_active
    )
    SELECT
        p_tenant_id, templates.id, templates.code, templates.standard_name,
        templates.account_class, templates.account_type, templates.normal_balance,
        templates.level, templates.is_detail, true, true
    FROM account_templates templates
    WHERE templates.localization_id = p_localization_id
      AND templates.is_active = true
    ON CONFLICT (tenant_id, code) DO NOTHING;

    UPDATE tenant_accounts child_account
    SET parent_id = parent_account.id
    FROM account_templates child_template
    JOIN account_templates parent_template
      ON parent_template.localization_id = child_template.localization_id
     AND parent_template.id = child_template.parent_template_id
    JOIN tenant_accounts parent_account
      ON parent_account.tenant_id = p_tenant_id
     AND parent_account.code = parent_template.code
    WHERE child_account.tenant_id = p_tenant_id
      AND child_account.template_id = child_template.id
      AND child_template.localization_id = p_localization_id
      AND child_account.parent_id IS DISTINCT FROM parent_account.id;

    IF p_localization_id = 'WARO_HOSPITALITY_GLOBAL_V1' THEN
        INSERT INTO expense_category_gl_mappings (
            tenant_id, category_code,
            debit_account_code, credit_cash_account_code, credit_default_account_code
        ) VALUES
            (p_tenant_id, 'SUPPLIES',     '6000', '1000', '2000'),
            (p_tenant_id, 'PAYROLL',      '5000', '1000', '1010'),
            (p_tenant_id, 'RENT',         '5000', '1000', '2000'),
            (p_tenant_id, 'UTILITIES',    '5000', '1000', '1010'),
            (p_tenant_id, 'MAINTENANCE',  '5000', '1000', '2000'),
            (p_tenant_id, 'MARKETING',    '5000', '1000', '1010'),
            (p_tenant_id, 'PROFESSIONAL', '5000', '1000', '1010'),
            (p_tenant_id, 'INSURANCE',    '5000', '1000', '1010'),
            (p_tenant_id, 'CAPITAL',      '5000', '1000', '2000'),
            (p_tenant_id, 'CONTINGENCY',  '5000', '1000', '1010')
        ON CONFLICT (tenant_id, category_code) DO NOTHING;
    ELSIF p_localization_id = 'WARO_CO_PUC_V1' THEN
        INSERT INTO expense_category_gl_mappings (
            tenant_id, category_code,
            debit_account_code, credit_cash_account_code, credit_default_account_code
        ) VALUES
            (p_tenant_id, 'SUPPLIES',     '6135', '1105', '2205'),
            (p_tenant_id, 'PAYROLL',      '5105', '1105', '2335'),
            (p_tenant_id, 'RENT',         '5140', '1105', '2205'),
            (p_tenant_id, 'UTILITIES',    '5135', '1105', '2335'),
            (p_tenant_id, 'MAINTENANCE',  '5145', '1105', '2205'),
            (p_tenant_id, 'MARKETING',    '5195', '1105', '2335'),
            (p_tenant_id, 'PROFESSIONAL', '5195', '1105', '2335'),
            (p_tenant_id, 'INSURANCE',    '5195', '1105', '2335'),
            (p_tenant_id, 'CAPITAL',      '5195', '1105', '2205'),
            (p_tenant_id, 'CONTINGENCY',  '5195', '1105', '2335')
        ON CONFLICT (tenant_id, category_code) DO NOTHING;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION seed_tenant_accounts(p_tenant_id UUID)
RETURNS VOID AS $$
DECLARE
    v_localization_id VARCHAR(50);
BEGIN
    SELECT accounting_localization
    INTO v_localization_id
    FROM tenant_financial_profiles
    WHERE tenant_id = p_tenant_id;

    IF v_localization_id IS NULL THEN
        RAISE EXCEPTION 'Tenant % has no financial profile', p_tenant_id;
    END IF;

    PERFORM seed_tenant_accounts(p_tenant_id, v_localization_id);
END;
$$ LANGUAGE plpgsql;

-- Backfill new accounts + role-mapped expense maps for existing hospitality tenants.
DO $$
DECLARE
    t_id UUID;
BEGIN
    FOR t_id IN
        SELECT tenant_id
        FROM tenant_financial_profiles
        WHERE accounting_localization = 'WARO_HOSPITALITY_GLOBAL_V1'
    LOOP
        PERFORM seed_tenant_accounts(t_id, 'WARO_HOSPITALITY_GLOBAL_V1');
    END LOOP;
END $$;

COMMIT;
