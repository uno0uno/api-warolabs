-- Issue #645: business profile completion enables an internal tenant session
-- while billing remains blocked until the tenant subscribes from Mi Plan.

-- Move eligible legacy rows to the new billing boundary first. Keeping the
-- tenant pending during the membership transition is required by the narrow
-- trigger exception defined in migration 110.
UPDATE tenant_onboarding o
SET state = 'payment_pending',
    updated_at = CASE
        WHEN o.state IS DISTINCT FROM 'payment_pending' THEN NOW()
        ELSE o.updated_at
    END
FROM tenants t
WHERE t.id = o.tenant_id
  AND t.lifecycle_status IN ('pending', 'active')
  AND o.state IN ('terms_pending', 'payment_pending')
  AND EXISTS (
      SELECT 1
      FROM tenant_financial_profiles fp
      WHERE fp.tenant_id = o.tenant_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM billing_payment_attempts a
      WHERE a.tenant_id = o.tenant_id
        AND a.status = 'approved'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_subscriptions s
      WHERE s.tenant_id = o.tenant_id
        AND s.status = 'active'
  );

-- Promote the bound self-service owner to the canonical internal admin role.
-- Re-running this statement also repairs an inactive admin left by a partial
-- deployment without touching unrelated tenant memberships.
UPDATE tenant_members tm
SET role = 'admin',
    is_active = true
FROM tenant_onboarding o, tenants t
WHERE t.id = o.tenant_id
  AND tm.tenant_id = o.tenant_id
  AND tm.user_id = o.owner_user_id
  AND tm.role IN ('owner', 'admin')
  AND t.lifecycle_status IN ('pending', 'active')
  AND o.state = 'payment_pending'
  AND EXISTS (
      SELECT 1
      FROM tenant_financial_profiles fp
      WHERE fp.tenant_id = o.tenant_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM billing_payment_attempts a
      WHERE a.tenant_id = o.tenant_id
        AND a.status = 'approved'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_subscriptions s
      WHERE s.tenant_id = o.tenant_id
        AND s.status = 'active'
  );

-- Activate the tenant last, after its internal membership is usable.
UPDATE tenants t
SET lifecycle_status = 'active'
FROM tenant_onboarding o
WHERE o.tenant_id = t.id
  AND t.lifecycle_status IN ('pending', 'active')
  AND o.state = 'payment_pending'
  AND EXISTS (
      SELECT 1
      FROM tenant_financial_profiles fp
      WHERE fp.tenant_id = t.id
  )
  AND EXISTS (
      SELECT 1
      FROM tenant_members tm
      WHERE tm.tenant_id = t.id
        AND tm.user_id = o.owner_user_id
        AND tm.role = 'admin'
        AND tm.is_active = true
  )
  AND NOT EXISTS (
      SELECT 1
      FROM billing_payment_attempts a
      WHERE a.tenant_id = t.id
        AND a.status = 'approved'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_subscriptions s
      WHERE s.tenant_id = t.id
        AND s.status = 'active'
  );
