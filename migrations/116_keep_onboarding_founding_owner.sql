-- Founding onboarding identity must stay superuser (platform "super usuario"),
-- matching legacy tenants. Do not demote to admin on activation.
-- Historical migrations 110/112 stay as applied history.

CREATE OR REPLACE FUNCTION enforce_tenant_owner_minimum()
RETURNS TRIGGER AS $$
DECLARE
    remaining_owners INT;
BEGIN
    IF NOT (OLD.role IN ('owner', 'superuser') AND OLD.is_active = true) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    -- Pending self-service onboarding may deactivate the bound founder while
    -- payment is incomplete. Role must remain owner/superuser (never demote to admin).
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
    'Protects the last active owner/superuser. Pending onboarding may deactivate the founder but must not demote them to admin.';

-- Pending founding membership uniqueness covers both legacy owner and superuser.
DROP INDEX IF EXISTS tenant_members_pending_owner_unique;
CREATE UNIQUE INDEX tenant_members_pending_owner_unique
  ON tenant_members (tenant_id, user_id)
  WHERE role IN ('owner', 'superuser');

-- Founding onboarding identity should be superuser (not admin or canonical owner).
UPDATE tenant_members tm
SET role = 'superuser'
FROM tenant_onboarding o
WHERE o.tenant_id = tm.tenant_id
  AND tm.user_id = o.owner_user_id
  AND tm.role IN ('admin', 'owner');
