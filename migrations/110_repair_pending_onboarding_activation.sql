-- Issue #1655: keep self-service identities pending until payment is approved
-- and repair rows activated by the previous terms-acceptance transition.

-- Migration 062 protects the last active owner. Self-service onboarding is the
-- narrow exception: its bound owner may be inactive while the tenant is pending,
-- and payment activation may replace that temporary role with canonical admin.
CREATE OR REPLACE FUNCTION enforce_tenant_owner_minimum()
RETURNS TRIGGER AS $$
DECLARE
    remaining_owners INT;
BEGIN
    IF NOT (OLD.role IN ('owner', 'superuser') AND OLD.is_active = true) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    IF TG_OP = 'UPDATE'
       AND NEW.tenant_id = OLD.tenant_id
       AND NEW.user_id = OLD.user_id
       AND (
           (NEW.role = 'owner' AND NEW.is_active = false)
           OR (NEW.role = 'admin' AND NEW.is_active = true)
       )
       AND EXISTS (
           SELECT 1
           FROM tenants t
           JOIN tenant_onboarding o
             ON o.tenant_id = t.id
            AND o.owner_user_id = OLD.user_id
           WHERE t.id = OLD.tenant_id
             AND t.lifecycle_status = 'pending'
             AND o.state NOT IN ('setup_complete', 'cancelled')
       )
    THEN
        RETURN NEW;
    END IF;

    SELECT COUNT(*) INTO remaining_owners
      FROM tenant_members
     WHERE tenant_id = OLD.tenant_id
       AND role IN ('owner', 'superuser')
       AND is_active = true
       AND id <> OLD.id;

    IF TG_OP = 'UPDATE'
       AND NEW.role IN ('owner', 'superuser')
       AND NEW.is_active = true
       AND NEW.tenant_id = OLD.tenant_id
    THEN
        remaining_owners := remaining_owners + 1;
    END IF;

    IF remaining_owners < 1 THEN
        RAISE EXCEPTION
            'tenant % must retain at least one active owner',
            OLD.tenant_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION enforce_tenant_owner_minimum() IS
    'Protects the last active owner, except the bound owner transitions of a pending self-service onboarding.';

-- Return the tenant to pending before deactivating its temporary owner so the
-- trigger exception is explicit and limited to the onboarding state machine.
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
        AND tm.is_active = true
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
  AND t.lifecycle_status = 'pending'
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
