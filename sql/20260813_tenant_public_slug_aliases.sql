-- api-warolabs#832: legacy onboarding-* slug → canonical redirects (contingency only)
CREATE TABLE IF NOT EXISTS tenant_public_slug_aliases (
    alias_slug TEXT PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_public_slug_aliases_tenant
    ON tenant_public_slug_aliases (tenant_id);
