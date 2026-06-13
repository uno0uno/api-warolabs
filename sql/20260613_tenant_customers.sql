-- Issue api-warolabs#429
-- Separate tenant customer relationship from internal team membership.

CREATE TABLE IF NOT EXISTS tenant_customers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    profile_id  UUID NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_customers_tenant_profile_unique UNIQUE (tenant_id, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_customers_profile_active
    ON tenant_customers (profile_id, tenant_id)
    WHERE is_active = true;

COMMENT ON TABLE tenant_customers IS
    'Explicit customer relationship per tenant/profile. Internal team roles remain in tenant_members.';

COMMENT ON COLUMN tenant_customers.profile_id IS
    'Customer profile identity; orders, wallet, and addresses stay linked to profile.id.';

INSERT INTO tenant_customers (tenant_id, profile_id, is_active)
SELECT tm.tenant_id, tm.user_id, COALESCE(tm.is_active, true)
FROM tenant_members tm
WHERE tm.role = 'customer'
  AND COALESCE(tm.is_active, true) = true
  AND tm.terminated_at IS NULL
ON CONFLICT (tenant_id, profile_id) DO UPDATE
SET is_active = true,
    updated_at = now();
