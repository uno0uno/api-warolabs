-- Migration 062: safeguard — every tenant must retain ≥1 active owner
-- Issue #163 — Epic 1 / Sub-task #E1.5
--
-- Defensive PG trigger preventing the membership table from ending up in a
-- state where a tenant has zero active owners. Without it, a UI bug or a
-- careless admin could demote/soft-delete the last owner and lock the whole
-- tenant out of administration. The trigger covers UPDATE and DELETE; INSERT
-- is unaffected (owner creation goes through tenants_service.create_tenant).
--
-- Transition guarantee: the trigger accepts BOTH the new canonical role
-- 'owner' and the legacy 'superuser' string while Epic 6's data migration is
-- pending. Once Epic 6 rewrites every superuser → owner, the OR clause can
-- be tightened in a follow-up migration.
--
-- Active filter: terminated members (is_active = false) do NOT count as
-- "still present", so soft-deleting the only owner is correctly blocked.
--
-- Idempotency: CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS makes
-- the migration safely re-runnable on dev.

CREATE OR REPLACE FUNCTION enforce_tenant_owner_minimum()
RETURNS TRIGGER AS $$
DECLARE
    remaining_owners INT;
BEGIN
    -- Skip when the row being changed is not (and was not) an active owner —
    -- there's nothing to protect in that case.
    IF NOT (OLD.role IN ('owner', 'superuser') AND OLD.is_active = true) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    -- Count active owners in this tenant excluding the row currently being
    -- touched (it's about to leave the active-owner set, conceptually).
    SELECT COUNT(*) INTO remaining_owners
      FROM tenant_members
     WHERE tenant_id = OLD.tenant_id
       AND role IN ('owner', 'superuser')
       AND is_active = true
       AND id <> OLD.id;

    -- For UPDATE: if NEW still leaves the row as an active owner of the same
    -- tenant, it counts toward the floor.
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


DROP TRIGGER IF EXISTS trg_enforce_tenant_owner_minimum ON tenant_members;

CREATE TRIGGER trg_enforce_tenant_owner_minimum
    BEFORE UPDATE OR DELETE ON tenant_members
    FOR EACH ROW
    EXECUTE FUNCTION enforce_tenant_owner_minimum();

COMMENT ON FUNCTION enforce_tenant_owner_minimum() IS
    'Blocks UPDATE/DELETE on tenant_members that would leave a tenant with zero active owners. Accepts owner and legacy superuser during the transition.';
