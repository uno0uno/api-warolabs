-- Migration 023: Create tenant_accounts table
-- Per-tenant chart of accounts, seeded from account_templates at company onboarding.
-- Tenants can rename accounts, add custom sub-accounts, disable unused accounts.

CREATE TABLE IF NOT EXISTS tenant_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    template_id UUID REFERENCES account_templates(id),
    code VARCHAR(10) NOT NULL,
    name VARCHAR(200) NOT NULL,
    account_class VARCHAR(1) NOT NULL
        CHECK (account_class IN ('1','2','3','4','5','6','7','8','9')),
    account_type VARCHAR(30) NOT NULL
        CHECK (account_type IN ('asset','liability','equity','income','expense','cogs','other')),
    normal_balance VARCHAR(6) NOT NULL
        CHECK (normal_balance IN ('debit','credit')),
    level INT NOT NULL CHECK (level IN (1,2,4,6,8)),
    parent_id UUID REFERENCES tenant_accounts(id),
    is_detail BOOLEAN NOT NULL DEFAULT false,
    is_system BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_account_code UNIQUE (tenant_id, code)
);

CREATE INDEX IF NOT EXISTS idx_tenant_accounts_tenant
    ON tenant_accounts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_accounts_tenant_code
    ON tenant_accounts(tenant_id, code);
CREATE INDEX IF NOT EXISTS idx_tenant_accounts_tenant_class
    ON tenant_accounts(tenant_id, account_class);
CREATE INDEX IF NOT EXISTS idx_tenant_accounts_parent
    ON tenant_accounts(parent_id);
CREATE INDEX IF NOT EXISTS idx_tenant_accounts_template
    ON tenant_accounts(template_id) WHERE template_id IS NOT NULL;

COMMENT ON TABLE tenant_accounts IS 'Per-tenant chart of accounts. Seeded from account_templates on company creation. Tenants may customize names and add auxiliary sub-accounts.';
COMMENT ON COLUMN tenant_accounts.template_id IS 'FK to account_templates. NULL for custom sub-accounts not in the PUC standard.';
COMMENT ON COLUMN tenant_accounts.is_system IS 'true = seeded from PUC template. false = custom account created by tenant.';
COMMENT ON COLUMN tenant_accounts.is_detail IS 'true = journal lines can post directly to this account (leaf node)';
