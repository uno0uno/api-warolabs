-- Migration 022: Create account_templates table
-- System-level PUC (Plan Único de Cuentas) reference table.
-- Shared across all tenants. Read-only for tenants.
-- Seeded separately in migration 022b (seed data).

CREATE TABLE IF NOT EXISTS account_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(10) NOT NULL UNIQUE,
    standard_name VARCHAR(200) NOT NULL,
    account_class VARCHAR(1) NOT NULL
        CHECK (account_class IN ('1','2','3','4','5','6','7','8','9')),
    account_type VARCHAR(30) NOT NULL
        CHECK (account_type IN ('asset','liability','equity','income','expense','cogs','other')),
    normal_balance VARCHAR(6) NOT NULL
        CHECK (normal_balance IN ('debit','credit')),
    level INT NOT NULL CHECK (level IN (1,2,4,6,8)),
    parent_code VARCHAR(10) REFERENCES account_templates(code),
    is_detail BOOLEAN NOT NULL DEFAULT false,
    niif_group VARCHAR(10),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_account_templates_parent
    ON account_templates(parent_code);
CREATE INDEX IF NOT EXISTS idx_account_templates_class
    ON account_templates(account_class);
CREATE INDEX IF NOT EXISTS idx_account_templates_active
    ON account_templates(is_active) WHERE is_active = true;

COMMENT ON TABLE account_templates IS 'System-level PUC colombiano account reference. Shared across all tenants. Never scoped by tenant_id.';
COMMENT ON COLUMN account_templates.code IS 'PUC account code: 1=class, 11=group, 1105=account, 110505=sub-account';
COMMENT ON COLUMN account_templates.level IS '1=class(1digit), 2=group(2digits), 4=account(4digits), 6=subaccount(6digits), 8=auxiliary(8digits)';
COMMENT ON COLUMN account_templates.is_detail IS 'true = journal lines can be posted directly to this account';
COMMENT ON COLUMN account_templates.niif_group IS 'grupo1, grupo2, grupo3 or NULL (applies to all)';
