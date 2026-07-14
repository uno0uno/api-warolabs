-- Issue #623: semantic account roles and tenant-scoped account bindings.
-- Codes remain during the compatibility window, but automatic producers resolve
-- account UUIDs from roles or explicit tenant-owned bindings.

BEGIN;

CREATE TABLE IF NOT EXISTS accounting_roles (
    role VARCHAR(60) PRIMARY KEY,
    colombia_only BOOLEAN NOT NULL DEFAULT false,
    description VARCHAR(240) NOT NULL
);

INSERT INTO accounting_roles (role, colombia_only, description) VALUES
    ('CASH', false, 'Cash settlement account'),
    ('BANK', false, 'Bank settlement account'),
    ('ACCOUNTS_RECEIVABLE', false, 'Customer receivable account'),
    ('INVENTORY', false, 'Inventory asset account'),
    ('ACCOUNTS_PAYABLE', false, 'Supplier payable account'),
    ('SALES_REVENUE', false, 'Sales revenue account'),
    ('TAX_PAYABLE', false, 'Generic tax payable account'),
    ('COGS', false, 'Cost of goods sold account'),
    ('PAYROLL_EXPENSE', false, 'Generic payroll expense account'),
    ('CUSTOMER_ADVANCES', false, 'Customer advances liability'),
    ('INC_PAYABLE', true, 'Colombia consumption tax payable'),
    ('IVA_PAYABLE', true, 'Colombia VAT payable'),
    ('LIQUOR_TAX_PAYABLE', true, 'Colombia liquor tax payable'),
    ('CONTRACTOR_EXPENSE', true, 'Colombia contractor expense'),
    ('WITHHOLDING_PAYABLE', true, 'Colombia withholding payable'),
    ('EMPLOYEE_SS_PAYABLE', true, 'Colombia employee social-security payable'),
    ('EMPLOYER_SS_PAYABLE', true, 'Colombia employer social-security payable'),
    ('EMPLOYER_SS_EXPENSE', true, 'Colombia employer social-security expense'),
    ('PRIMA_EXPENSE', true, 'Colombia service bonus provision expense'),
    ('PRIMA_PAYABLE', true, 'Colombia service bonus payable'),
    ('CESANTIAS_EXPENSE', true, 'Colombia severance provision expense'),
    ('CESANTIAS_PAYABLE', true, 'Colombia severance payable'),
    ('CESANTIAS_INTEREST_EXPENSE', true, 'Colombia severance interest expense'),
    ('CESANTIAS_INTEREST_PAYABLE', true, 'Colombia severance interest payable'),
    ('VACATION_EXPENSE', true, 'Colombia vacation provision expense'),
    ('VACATION_PAYABLE', true, 'Colombia vacation payable'),
    ('OVERTIME_EXPENSE', true, 'Colombia overtime expense'),
    ('DOTACION_EXPENSE', true, 'Colombia work-clothing expense'),
    ('TERMINATION_EXPENSE', true, 'Colombia termination expense'),
    ('BANK_FEES_EXPENSE', false, 'Bank fees and settlement differences'),
    ('OTHER_INCOME', false, 'Other operating or non-operating income')
ON CONFLICT (role) DO UPDATE SET
    colombia_only = EXCLUDED.colombia_only,
    description = EXCLUDED.description;

ALTER TABLE account_template_role_defaults
    DROP CONSTRAINT IF EXISTS account_template_role_defaults_role_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'account_template_role_defaults'::regclass
          AND conname = 'fk_account_template_role_catalog'
    ) THEN
        ALTER TABLE account_template_role_defaults
            ADD CONSTRAINT fk_account_template_role_catalog
            FOREIGN KEY (role) REFERENCES accounting_roles(role);
    END IF;
END $$;

-- Historical payroll migrations created some tenant rows without template_id.
-- Link only CO rows whose code matches the localized template; IDs and names stay intact.
UPDATE tenant_accounts accounts
SET template_id = templates.id
FROM tenant_financial_profiles profiles
JOIN account_templates templates
  ON templates.localization_id = profiles.accounting_localization
WHERE profiles.tenant_id = accounts.tenant_id
  AND profiles.accounting_localization = 'WARO_CO_PUC_V1'
  AND templates.code = accounts.code
  AND accounts.template_id IS NULL;

WITH role_codes(role, code) AS (
    VALUES
        ('INC_PAYABLE', '2495'),
        ('IVA_PAYABLE', '2408'),
        ('LIQUOR_TAX_PAYABLE', '2408'),
        ('CONTRACTOR_EXPENSE', '5199'),
        ('WITHHOLDING_PAYABLE', '2367'),
        ('EMPLOYEE_SS_PAYABLE', '237005'),
        ('EMPLOYER_SS_PAYABLE', '237010'),
        ('EMPLOYER_SS_EXPENSE', '5120'),
        ('PRIMA_EXPENSE', '5106'),
        ('PRIMA_PAYABLE', '2620'),
        ('CESANTIAS_EXPENSE', '5107'),
        ('CESANTIAS_PAYABLE', '2610'),
        ('CESANTIAS_INTEREST_EXPENSE', '5108'),
        ('CESANTIAS_INTEREST_PAYABLE', '2615'),
        ('VACATION_EXPENSE', '5109'),
        ('VACATION_PAYABLE', '2625'),
        ('OVERTIME_EXPENSE', '5110'),
        ('DOTACION_EXPENSE', '5115'),
        ('TERMINATION_EXPENSE', '5198'),
        ('BANK_FEES_EXPENSE', '5305'),
        ('OTHER_INCOME', '4295')
)
INSERT INTO account_template_role_defaults (localization_id, role, account_template_id)
SELECT 'WARO_CO_PUC_V1', role_codes.role, templates.id
FROM role_codes
JOIN account_templates templates
  ON templates.localization_id = 'WARO_CO_PUC_V1'
 AND templates.code = role_codes.code
ON CONFLICT (localization_id, role) DO UPDATE
SET account_template_id = EXCLUDED.account_template_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'tenant_accounts'::regclass
          AND conname = 'uq_tenant_accounts_tenant_id_id'
    ) THEN
        ALTER TABLE tenant_accounts
            ADD CONSTRAINT uq_tenant_accounts_tenant_id_id UNIQUE (tenant_id, id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS tenant_account_role_overrides (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role VARCHAR(60) NOT NULL REFERENCES accounting_roles(role),
    tenant_account_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, role),
    CONSTRAINT fk_role_override_tenant_account
        FOREIGN KEY (tenant_id, tenant_account_id)
        REFERENCES tenant_accounts(tenant_id, id)
);

ALTER TABLE payment_methods ADD COLUMN IF NOT EXISTS gl_account_id UUID;
ALTER TABLE payment_method_groups ADD COLUMN IF NOT EXISTS gl_account_id UUID;
ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS inc_gl_account_id UUID,
    ADD COLUMN IF NOT EXISTS iva_gl_account_id UUID,
    ADD COLUMN IF NOT EXISTS liquor_tax_gl_account_id UUID;

UPDATE payment_methods methods
SET gl_account_id = accounts.id
FROM tenant_accounts accounts
WHERE accounts.tenant_id = methods.tenant_id
  AND accounts.code = methods.gl_account_code
  AND methods.gl_account_id IS NULL;

UPDATE payment_method_groups groups
SET gl_account_id = accounts.id
FROM tenant_accounts accounts
WHERE groups.tenant_id IS NOT NULL
  AND accounts.tenant_id = groups.tenant_id
  AND accounts.code = groups.gl_account_code
  AND groups.gl_account_id IS NULL;

UPDATE tenant_tax_config config
SET inc_gl_account_id = accounts.id
FROM tenant_accounts accounts
WHERE accounts.tenant_id = config.tenant_id
  AND accounts.code = config.inc_gl_account_code
  AND config.inc_gl_account_id IS NULL;

UPDATE tenant_tax_config config
SET iva_gl_account_id = accounts.id
FROM tenant_accounts accounts
WHERE accounts.tenant_id = config.tenant_id
  AND accounts.code = config.iva_gl_account_code
  AND config.iva_gl_account_id IS NULL;

UPDATE tenant_tax_config config
SET liquor_tax_gl_account_id = accounts.id
FROM tenant_accounts accounts
WHERE accounts.tenant_id = config.tenant_id
  AND accounts.code = config.liquor_tax_gl_account_code
  AND config.liquor_tax_gl_account_id IS NULL;

INSERT INTO tenant_account_role_overrides (tenant_id, role, tenant_account_id)
SELECT profiles.tenant_id, 'CUSTOMER_ADVANCES', accounts.id
FROM tenant_public_profiles profiles
JOIN tenant_accounts accounts
  ON accounts.tenant_id = profiles.tenant_id
 AND accounts.code = profiles.customer_wallet_liability_gl_code
ON CONFLICT (tenant_id, role) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_payment_method_tenant_account') THEN
        ALTER TABLE payment_methods ADD CONSTRAINT fk_payment_method_tenant_account
            FOREIGN KEY (tenant_id, gl_account_id) REFERENCES tenant_accounts(tenant_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_payment_group_tenant_account') THEN
        ALTER TABLE payment_method_groups ADD CONSTRAINT fk_payment_group_tenant_account
            FOREIGN KEY (tenant_id, gl_account_id) REFERENCES tenant_accounts(tenant_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tax_inc_tenant_account') THEN
        ALTER TABLE tenant_tax_config ADD CONSTRAINT fk_tax_inc_tenant_account
            FOREIGN KEY (tenant_id, inc_gl_account_id) REFERENCES tenant_accounts(tenant_id, id);
        ALTER TABLE tenant_tax_config ADD CONSTRAINT fk_tax_iva_tenant_account
            FOREIGN KEY (tenant_id, iva_gl_account_id) REFERENCES tenant_accounts(tenant_id, id);
        ALTER TABLE tenant_tax_config ADD CONSTRAINT fk_tax_liquor_tenant_account
            FOREIGN KEY (tenant_id, liquor_tax_gl_account_id) REFERENCES tenant_accounts(tenant_id, id);
    END IF;
END $$;

ALTER TABLE payment_method_groups
    DROP CONSTRAINT IF EXISTS chk_payment_group_tenant_account_scope;
ALTER TABLE payment_method_groups
    ADD CONSTRAINT chk_payment_group_tenant_account_scope
    CHECK (gl_account_id IS NULL OR tenant_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_role_overrides_account
    ON tenant_account_role_overrides(tenant_account_id);

COMMENT ON TABLE tenant_account_role_overrides IS
    'Tenant-owned semantic account overrides. Localization template defaults are the fallback.';
COMMENT ON COLUMN payment_methods.gl_account_code IS
    'Deprecated compatibility field; automatic posting uses gl_account_id.';
COMMENT ON COLUMN payment_method_groups.gl_account_code IS
    'Deprecated compatibility field; automatic posting uses gl_account_id or semantic group role.';

COMMIT;
