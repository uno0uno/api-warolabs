-- Migration 022: Create versioned accounting localizations and templates.
-- Migration 104 upgrades databases that already ran the original CO-only form.

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
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS account_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    localization_id VARCHAR(50) NOT NULL DEFAULT 'WARO_CO_PUC_V1',
    code VARCHAR(10) NOT NULL UNIQUE,
    standard_name VARCHAR(200) NOT NULL,
    account_class VARCHAR(1) NOT NULL
        CHECK (account_class IN ('1','2','3','4','5','6','7','8','9')),
    account_type VARCHAR(30) NOT NULL
        CHECK (account_type IN ('asset','liability','equity','income','expense','cogs','other')),
    normal_balance VARCHAR(6) NOT NULL
        CHECK (normal_balance IN ('debit','credit')),
    level INT NOT NULL CHECK (level IN (1,2,4,6,8)),
    -- parent_code remains as bootstrap metadata. Migration 104 removes its
    -- global FK once repeated codes exist and parent_template_id is populated.
    parent_code VARCHAR(10) REFERENCES account_templates(code),
    parent_template_id UUID,
    is_detail BOOLEAN NOT NULL DEFAULT false,
    niif_group VARCHAR(10),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_account_templates_localization_code
        UNIQUE (localization_id, code),
    CONSTRAINT uq_account_templates_localization_id_id
        UNIQUE (localization_id, id),
    CONSTRAINT fk_account_templates_localization
        FOREIGN KEY (localization_id)
        REFERENCES accounting_localizations(id),
    CONSTRAINT fk_account_templates_localized_parent
        FOREIGN KEY (localization_id, parent_template_id)
        REFERENCES account_templates(localization_id, id)
);

CREATE INDEX IF NOT EXISTS idx_account_templates_parent
    ON account_templates(parent_template_id);
CREATE INDEX IF NOT EXISTS idx_account_templates_localization_parent_code
    ON account_templates(localization_id, parent_code);
CREATE INDEX IF NOT EXISTS idx_account_templates_class
    ON account_templates(localization_id, account_class);
CREATE INDEX IF NOT EXISTS idx_account_templates_active
    ON account_templates(localization_id, is_active) WHERE is_active = true;

COMMENT ON TABLE account_templates IS 'Versioned system account templates. Tenant charts copy exactly one accounting localization.';
COMMENT ON COLUMN account_templates.localization_id IS 'Accounting localization and version owning this template row.';
COMMENT ON COLUMN account_templates.code IS 'Account code unique inside one accounting localization.';
COMMENT ON COLUMN account_templates.parent_template_id IS 'Localized UUID hierarchy; never resolved globally by code.';
COMMENT ON COLUMN account_templates.level IS '1=class(1digit), 2=group(2digits), 4=account(4digits), 6=subaccount(6digits), 8=auxiliary(8digits)';
COMMENT ON COLUMN account_templates.is_detail IS 'true = journal lines can be posted directly to this account';
COMMENT ON COLUMN account_templates.niif_group IS 'grupo1, grupo2, grupo3 or NULL (applies to all)';
