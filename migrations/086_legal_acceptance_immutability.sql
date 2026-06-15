-- Migration 086: make legal acceptance evidence append-only
--
-- TyC acceptance rows are electronic evidence. They must remain available for
-- audit/legal review for at least 10 years, so normal application changes must
-- not update or delete the accepted snapshot after insert.

CREATE OR REPLACE FUNCTION prevent_tenant_legal_acceptance_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'tenant_legal_acceptances rows are immutable legal evidence'
        USING ERRCODE = '45000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tenant_legal_acceptances_immutable
    ON tenant_legal_acceptances;

CREATE TRIGGER trg_tenant_legal_acceptances_immutable
BEFORE UPDATE OR DELETE ON tenant_legal_acceptances
FOR EACH ROW
EXECUTE FUNCTION prevent_tenant_legal_acceptance_mutation();

COMMENT ON FUNCTION prevent_tenant_legal_acceptance_mutation() IS
    'Blocks UPDATE/DELETE on tenant_legal_acceptances; retain acceptance evidence for at least 10 years.';
