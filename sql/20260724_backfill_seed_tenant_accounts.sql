-- One-shot backfill: active tenants with a financial profile and zero tenant_accounts.
-- Idempotent via seed_tenant_accounts (ON CONFLICT DO NOTHING).
-- Safe to re-run.

DO $$
DECLARE
    r RECORD;
    seeded INT := 0;
BEGIN
    FOR r IN
        SELECT t.id
        FROM tenants t
        JOIN tenant_financial_profiles tfp ON tfp.tenant_id = t.id
        WHERE t.lifecycle_status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM tenant_accounts ta WHERE ta.tenant_id = t.id
          )
        ORDER BY t.created_at
    LOOP
        PERFORM seed_tenant_accounts(r.id);
        seeded := seeded + 1;
    END LOOP;

    RAISE NOTICE 'seed_tenant_accounts backfill complete: % tenants', seeded;
END $$;
