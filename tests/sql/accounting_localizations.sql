\set ON_ERROR_STOP on

BEGIN;

-- The script expects a disposable database with migrations through 103. It is
-- transaction-wrapped and leaves no fixtures or migration changes behind.
\ir ../../migrations/104_accounting_localizations.sql

CREATE TEMP TABLE co_templates_before AS
SELECT id, code, standard_name, parent_code, parent_template_id
FROM account_templates
WHERE localization_id = 'WARO_CO_PUC_V1';

CREATE TEMP TABLE journal_lines_before AS
SELECT id, account_id
FROM tenant_journal_lines;

-- A second application must be a no-op for identities and historical links.
\ir ../../migrations/104_accounting_localizations.sql

DO $$
BEGIN
    IF EXISTS (
        (SELECT id, code, standard_name, parent_code, parent_template_id
         FROM co_templates_before
         EXCEPT
         SELECT id, code, standard_name, parent_code, parent_template_id
         FROM account_templates
         WHERE localization_id = 'WARO_CO_PUC_V1')
        UNION ALL
        (SELECT id, code, standard_name, parent_code, parent_template_id
         FROM account_templates
         WHERE localization_id = 'WARO_CO_PUC_V1'
         EXCEPT
         SELECT id, code, standard_name, parent_code, parent_template_id
         FROM co_templates_before)
    ) THEN
        RAISE EXCEPTION 'CO templates changed on idempotent migration';
    END IF;

    IF EXISTS (
        (SELECT id, account_id FROM journal_lines_before
         EXCEPT SELECT id, account_id FROM tenant_journal_lines)
        UNION ALL
        (SELECT id, account_id FROM tenant_journal_lines
         EXCEPT SELECT id, account_id FROM journal_lines_before)
    ) THEN
        RAISE EXCEPTION 'Historical journal account_id changed';
    END IF;

    IF (SELECT COUNT(*) FROM account_template_role_defaults
        WHERE localization_id = 'WARO_CO_PUC_V1') <> 9 THEN
        RAISE EXCEPTION 'CO must have nine generic defaults; tax remains specific';
    END IF;

    IF (SELECT COUNT(*) FROM account_template_role_defaults
        WHERE localization_id = 'WARO_HOSPITALITY_GLOBAL_V1') <> 12 THEN
        RAISE EXCEPTION 'Global must have twelve semantic defaults';
    END IF;
END $$;

-- Temp tables shadow production tenant data while exercising the real function.
CREATE TEMP TABLE tenant_financial_profiles (
    tenant_id UUID PRIMARY KEY,
    accounting_localization VARCHAR(50) NOT NULL
);

CREATE TEMP TABLE tenant_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    template_id UUID,
    code VARCHAR(10) NOT NULL,
    name VARCHAR(200) NOT NULL,
    account_class VARCHAR(1) NOT NULL,
    account_type VARCHAR(30) NOT NULL,
    normal_balance VARCHAR(6) NOT NULL,
    level INT NOT NULL,
    parent_id UUID,
    is_detail BOOLEAN NOT NULL DEFAULT false,
    is_system BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (tenant_id, code)
);

INSERT INTO tenant_financial_profiles VALUES
    ('00000000-0000-0000-0000-000000000624', 'WARO_CO_PUC_V1'),
    ('00000000-0000-0000-0000-000000000625', 'WARO_HOSPITALITY_GLOBAL_V1');

INSERT INTO tenant_accounts (
    tenant_id, template_id, code, name, account_class, account_type,
    normal_balance, level, is_detail, is_system, is_active
) VALUES (
    '00000000-0000-0000-0000-000000000625', NULL, '1000', 'Custom cash',
    '1', 'asset', 'debit', 4, true, false, true
);

SELECT seed_tenant_accounts(
    '00000000-0000-0000-0000-000000000624', 'WARO_CO_PUC_V1'
);
SELECT seed_tenant_accounts(
    '00000000-0000-0000-0000-000000000625', 'WARO_HOSPITALITY_GLOBAL_V1'
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM tenant_accounts accounts
        JOIN account_templates templates ON templates.id = accounts.template_id
        WHERE accounts.tenant_id = '00000000-0000-0000-0000-000000000624'
          AND templates.localization_id <> 'WARO_CO_PUC_V1'
    ) THEN
        RAISE EXCEPTION 'CO tenant received another localization';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM tenant_accounts accounts
        JOIN account_templates templates ON templates.id = accounts.template_id
        WHERE accounts.tenant_id = '00000000-0000-0000-0000-000000000625'
          AND templates.localization_id <> 'WARO_HOSPITALITY_GLOBAL_V1'
    ) THEN
        RAISE EXCEPTION 'Global tenant received CO templates';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM tenant_accounts
        WHERE tenant_id = '00000000-0000-0000-0000-000000000625'
          AND code = '1000' AND name = 'Custom cash' AND template_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Custom conflicting account was overwritten';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM tenant_accounts accounts
        JOIN account_templates templates ON templates.id = accounts.template_id
        WHERE accounts.tenant_id IN (
            '00000000-0000-0000-0000-000000000624',
            '00000000-0000-0000-0000-000000000625'
        )
          AND templates.parent_template_id IS NOT NULL
          AND accounts.parent_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Seeded hierarchy contains an orphan';
    END IF;

    BEGIN
        PERFORM seed_tenant_accounts(
            '00000000-0000-0000-0000-000000000625', 'WARO_CO_PUC_V1'
        );
        RAISE EXCEPTION 'Mismatched localization was accepted';
    EXCEPTION
        WHEN raise_exception THEN
            IF SQLERRM = 'Mismatched localization was accepted' THEN
                RAISE;
            END IF;
    END;
END $$;

ROLLBACK;
