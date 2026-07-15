-- Issue #1655: keep self-service identities pending until payment is approved
-- and repair rows activated by the previous terms-acceptance transition.

-- Revoke premature membership access only when there is no trusted payment or
-- active subscription evidence. Every statement is predicate-guarded so a
-- partial execution can be rerun safely.
UPDATE tenant_members tm
SET is_active = false
FROM tenant_onboarding o, tenants t
WHERE o.tenant_id = t.id
  AND tm.tenant_id = t.id
  AND tm.user_id = o.owner_user_id
  AND tm.role = 'owner'
  AND tm.is_active = true
  AND t.lifecycle_status = 'active'
  AND o.state = 'payment_pending'
  AND NOT EXISTS (
      SELECT 1
      FROM billing_payment_attempts a
      WHERE a.tenant_id = t.id AND a.status = 'approved'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_subscriptions s
      WHERE s.tenant_id = t.id AND s.status = 'active'
  );

UPDATE tenants t
SET lifecycle_status = 'pending'
FROM tenant_onboarding o
WHERE o.tenant_id = t.id
  AND t.lifecycle_status = 'active'
  AND o.state = 'payment_pending'
  AND EXISTS (
      SELECT 1
      FROM tenant_members tm
      WHERE tm.tenant_id = t.id
        AND tm.user_id = o.owner_user_id
        AND tm.role = 'owner'
        AND tm.is_active = false
  )
  AND NOT EXISTS (
      SELECT 1
      FROM billing_payment_attempts a
      WHERE a.tenant_id = t.id AND a.status = 'approved'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_subscriptions s
      WHERE s.tenant_id = t.id AND s.status = 'active'
  );

-- Paid self-service owners use the established internal admin role after
-- activation. This also repairs paid rows created before this migration.
UPDATE tenant_members tm
SET role = 'admin', is_active = true
FROM tenant_onboarding o, tenants t
WHERE o.tenant_id = t.id
  AND tm.tenant_id = t.id
  AND tm.user_id = o.owner_user_id
  AND tm.role = 'owner'
  AND t.lifecycle_status = 'active'
  AND o.state IN ('paid', 'active', 'setup_complete')
  AND (
      EXISTS (
          SELECT 1
          FROM billing_payment_attempts a
          WHERE a.tenant_id = t.id AND a.status = 'approved'
      )
      OR EXISTS (
          SELECT 1
          FROM tenant_subscriptions s
          WHERE s.tenant_id = t.id AND s.status = 'active'
      )
  );
