-- Issue #624: versioned accounting localizations and localized account templates.
-- Existing PUC template IDs and tenant account references are preserved.

CREATE TABLE IF NOT EXISTS accounting_localizations (
    id VARCHAR(50) PRIMARY KEY,
    country_code CHAR(2),
    version INT NOT NULL CHECK (version > 0),
    display_name VARCHAR(120) NOT NULL,
    description TEXT NOT NULL,
    is_fiscal BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT accounting_localizations_country_scope CHECK (
        (id = 'WARO_CO_PUC_V1' AND country_code = 'CO' AND is_fiscal = true)
        OR
        (id = 'WARO_HOSPITALITY_GLOBAL_V1' AND country_code IS NULL AND is_fiscal = false)
    )
);

INSERT INTO accounting_localizations (
    id, country_code, version, display_name, description, is_fiscal, is_active
) VALUES
    (
        'WARO_CO_PUC_V1', 'CO', 1, 'WARO Colombia PUC',
        'Plan de cuentas colombiano usado por la operacion fiscal de WARO en Colombia.',
        true, true
    ),
    (
        'WARO_HOSPITALITY_GLOBAL_V1', NULL, 1, 'WARO Hospitality Global',
        'Plantilla gerencial no fiscal; no representa NIIF, GAAP ni cumplimiento legal local.',
        false, true
    )
ON CONFLICT (id) DO UPDATE SET
    country_code = EXCLUDED.country_code,
    version = EXCLUDED.version,
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    is_fiscal = EXCLUDED.is_fiscal,
    is_active = EXCLUDED.is_active;

ALTER TABLE account_templates
    ADD COLUMN IF NOT EXISTS localization_id VARCHAR(50),
    ADD COLUMN IF NOT EXISTS parent_template_id UUID;

-- Every pre-existing row is the Colombian PUC. This update changes neither its
-- primary key nor any tenant_accounts.template_id reference.
UPDATE account_templates
SET localization_id = 'WARO_CO_PUC_V1'
WHERE localization_id IS NULL;

ALTER TABLE account_templates
    ALTER COLUMN localization_id SET DEFAULT 'WARO_CO_PUC_V1',
    ALTER COLUMN localization_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'account_templates'::regclass
          AND conname = 'fk_account_templates_localization'
    ) THEN
        ALTER TABLE account_templates
            ADD CONSTRAINT fk_account_templates_localization
            FOREIGN KEY (localization_id) REFERENCES accounting_localizations(id);
    END IF;
END $$;

-- Remove identities that assume code is globally unique before adding Global.
ALTER TABLE account_templates
    DROP CONSTRAINT IF EXISTS account_templates_parent_code_fkey,
    DROP CONSTRAINT IF EXISTS account_templates_code_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'account_templates'::regclass
          AND conname = 'uq_account_templates_localization_code'
    ) THEN
        ALTER TABLE account_templates
            ADD CONSTRAINT uq_account_templates_localization_code
            UNIQUE (localization_id, code);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'account_templates'::regclass
          AND conname = 'uq_account_templates_localization_id_id'
    ) THEN
        ALTER TABLE account_templates
            ADD CONSTRAINT uq_account_templates_localization_id_id
            UNIQUE (localization_id, id);
    END IF;
END $$;

UPDATE account_templates child
SET parent_template_id = parent.id
FROM account_templates parent
WHERE parent.localization_id = child.localization_id
  AND parent.code = child.parent_code
  AND child.parent_code IS NOT NULL
  AND child.parent_template_id IS DISTINCT FROM parent.id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'account_templates'::regclass
          AND conname = 'fk_account_templates_localized_parent'
    ) THEN
        ALTER TABLE account_templates
            ADD CONSTRAINT fk_account_templates_localized_parent
            FOREIGN KEY (localization_id, parent_template_id)
            REFERENCES account_templates(localization_id, id);
    END IF;
END $$;

DROP INDEX IF EXISTS idx_account_templates_parent;
CREATE INDEX IF NOT EXISTS idx_account_templates_parent
    ON account_templates(parent_template_id);
CREATE INDEX IF NOT EXISTS idx_account_templates_localization_parent_code
    ON account_templates(localization_id, parent_code);
CREATE INDEX IF NOT EXISTS idx_account_templates_localization_class
    ON account_templates(localization_id, account_class);
CREATE INDEX IF NOT EXISTS idx_account_templates_localization_active
    ON account_templates(localization_id, is_active) WHERE is_active = true;

-- Minimal non-statutory hospitality chart. Repeated class codes are deliberate:
-- uniqueness and hierarchy are local to WARO_HOSPITALITY_GLOBAL_V1.
INSERT INTO account_templates (
    localization_id, code, standard_name, account_class, account_type,
    normal_balance, level, parent_code, is_detail, niif_group, is_active
) VALUES
    ('WARO_HOSPITALITY_GLOBAL_V1', '1',    'Assets',                     '1', 'asset',     'debit',  1, NULL, false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '10',   'Current assets',             '1', 'asset',     'debit',  2, '1',  false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '1000', 'Cash',                       '1', 'asset',     'debit',  4, '10', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '1010', 'Bank',                       '1', 'asset',     'debit',  4, '10', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '1100', 'Accounts receivable',        '1', 'asset',     'debit',  4, '10', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '1200', 'Inventory',                  '1', 'asset',     'debit',  4, '10', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '2',    'Liabilities',                '2', 'liability', 'credit', 1, NULL, false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '20',   'Current liabilities',        '2', 'liability', 'credit', 2, '2',  false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '2000', 'Accounts payable',           '2', 'liability', 'credit', 4, '20', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '2100', 'Tax payable',                '2', 'liability', 'credit', 4, '20', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '2200', 'Customer advances',          '2', 'liability', 'credit', 4, '20', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '4',    'Revenue',                    '4', 'income',    'credit', 1, NULL, false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '40',   'Operating revenue',          '4', 'income',    'credit', 2, '4',  false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '4000', 'Sales revenue',              '4', 'income',    'credit', 4, '40', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '4010', 'Other income',               '4', 'income',    'credit', 4, '40', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '5',    'Expenses',                   '5', 'expense',   'debit',  1, NULL, false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '50',   'Operating expenses',         '5', 'expense',   'debit',  2, '5',  false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '5000', 'Payroll expense',            '5', 'expense',   'debit',  4, '50', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '5100', 'Bank fees expense',          '5', 'expense',   'debit',  4, '50', true,  NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '6',    'Cost of goods sold',         '6', 'cogs',      'debit',  1, NULL, false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '60',   'Cost of goods sold details', '6', 'cogs',      'debit',  2, '6',  false, NULL, true),
    ('WARO_HOSPITALITY_GLOBAL_V1', '6000', 'Cost of goods sold',         '6', 'cogs',      'debit',  4, '60', true,  NULL, true)
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
  AND child.parent_code IS NOT NULL
  AND child.parent_template_id IS DISTINCT FROM parent.id;

CREATE TABLE IF NOT EXISTS account_template_role_defaults (
    localization_id VARCHAR(50) NOT NULL,
    role VARCHAR(40) NOT NULL CHECK (role IN (
        'CASH', 'BANK', 'ACCOUNTS_RECEIVABLE', 'INVENTORY',
        'ACCOUNTS_PAYABLE', 'SALES_REVENUE', 'TAX_PAYABLE', 'COGS',
        'PAYROLL_EXPENSE', 'CUSTOMER_ADVANCES',
        'BANK_FEES_EXPENSE', 'OTHER_INCOME'
    )),
    account_template_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (localization_id, role),
    CONSTRAINT fk_account_template_role_localization
        FOREIGN KEY (localization_id) REFERENCES accounting_localizations(id),
    CONSTRAINT fk_account_template_role_template
        FOREIGN KEY (localization_id, account_template_id)
        REFERENCES account_templates(localization_id, id)
);

WITH role_codes(role, code) AS (
    VALUES
        ('CASH', '1105'),
        ('BANK', '1110'),
        ('ACCOUNTS_RECEIVABLE', '1305'),
        ('INVENTORY', '1435'),
        ('ACCOUNTS_PAYABLE', '2205'),
        ('SALES_REVENUE', '4175'),
        ('COGS', '6135'),
        ('PAYROLL_EXPENSE', '5105'),
        ('CUSTOMER_ADVANCES', '2810')
)
INSERT INTO account_template_role_defaults (localization_id, role, account_template_id)
SELECT 'WARO_CO_PUC_V1', role_codes.role, templates.id
FROM role_codes
JOIN account_templates templates
  ON templates.localization_id = 'WARO_CO_PUC_V1'
 AND templates.code = role_codes.code
ON CONFLICT (localization_id, role) DO UPDATE
SET account_template_id = EXCLUDED.account_template_id;

WITH role_codes(role, code) AS (
    VALUES
        ('CASH', '1000'),
        ('BANK', '1010'),
        ('ACCOUNTS_RECEIVABLE', '1100'),
        ('INVENTORY', '1200'),
        ('ACCOUNTS_PAYABLE', '2000'),
        ('TAX_PAYABLE', '2100'),
        ('CUSTOMER_ADVANCES', '2200'),
        ('SALES_REVENUE', '4000'),
        ('OTHER_INCOME', '4010'),
        ('PAYROLL_EXPENSE', '5000'),
        ('BANK_FEES_EXPENSE', '5100'),
        ('COGS', '6000')
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
END;
$$ LANGUAGE plpgsql;

-- Compatibility entry point for callers that already know only the tenant.
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

COMMENT ON TABLE accounting_localizations IS
    'Versioned accounting template catalogs. Global is managerial and non-statutory.';
COMMENT ON TABLE account_template_role_defaults IS
    'Semantic role defaults per localization; tax-specific CO bindings remain separate.';
COMMENT ON COLUMN account_templates.parent_code IS
    'Human-readable bootstrap metadata; parent_template_id is the authoritative hierarchy.';
COMMENT ON COLUMN account_templates.parent_template_id IS
    'Parent template UUID constrained to the same accounting localization.';
