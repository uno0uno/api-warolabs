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
SELECT source.tenant_id, source.profile_id, true
FROM (
    SELECT tm.tenant_id, tm.user_id AS profile_id
    FROM tenant_members tm
    WHERE tm.role = 'customer'
      AND COALESCE(tm.is_active, true) = true
      AND tm.terminated_at IS NULL

    UNION

    SELECT o.tenant_id, o.customer_id AS profile_id
    FROM orders o
    WHERE o.customer_id IS NOT NULL

    UNION

    SELECT cwb.tenant_id, cwb.profile_id
    FROM customer_wallet_balances cwb

    UNION

    SELECT cwm.tenant_id, cwm.profile_id
    FROM customer_wallet_movements cwm

    UNION

    SELECT ww.tenant_id, ww.profile_id
    FROM waros_wallets ww

    UNION

    SELECT wt.tenant_id, wt.profile_id
    FROM waros_transactions wt

    UNION

    SELECT oc.tenant_id, oc.customer_id AS profile_id
    FROM online_carts oc
    WHERE oc.customer_id IS NOT NULL
) AS source
ON CONFLICT (tenant_id, profile_id) DO UPDATE
SET is_active = true,
    updated_at = now();

UPDATE sessions s
SET is_active = false,
    ended_at = now(),
    end_reason = 'customer_role_denied'
FROM tenant_members tm
WHERE tm.tenant_id = s.tenant_id
  AND tm.user_id = s.user_id
  AND tm.is_active = true
  AND tm.role = 'customer'
  AND s.is_active = true
  AND s.expires_at > now();
