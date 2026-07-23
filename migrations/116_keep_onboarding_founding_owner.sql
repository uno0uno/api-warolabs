-- Keep the founding onboarding member as owner (super usuario).
-- Previous activation paths demoted owner → admin, which hid equipo/mi_negocio.
-- Historical migrations 110/112 stay as applied history; this updates the live
-- safeguard and backfills founding members still stuck as admin.

CREATE OR REPLACE FUNCTION enforce_tenant_owner_minimum()
RETURNS TRIGGER AS $$
DECLARE
    remaining_owners INT;
BEGIN
    IF NOT (OLD.role IN ('owner', 'superuser') AND OLD.is_active = true) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    -- Pending self-service onboarding may deactivate the bound owner while
    -- payment is incomplete. Role must remain owner (never demote to admin).
    IF TG_OP = 'UPDATE'
       AND NEW.tenant_id = OLD.tenant_id
       AND NEW.user_id = OLD.user_id
       AND NEW.role IN ('owner', 'superuser')
       AND NEW.is_active = false
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
    'Protects the last active owner. Pending onboarding may deactivate the bound owner but must not demote them to admin.';

-- Founding onboarding identity should be owner, not admin.
UPDATE tenant_members tm
SET role = 'owner'
FROM tenant_onboarding o
WHERE o.tenant_id = tm.tenant_id
  AND tm.user_id = o.owner_user_id
  AND tm.role = 'admin';
