-- Run after migrations with: psql "$TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -f tests/sql/account_role_bindings.sql
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'tenant_account_role_overrides'
    ) THEN
        RAISE EXCEPTION 'tenant_account_role_overrides is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payment_methods' AND column_name = 'gl_account_id'
    ) THEN
        RAISE EXCEPTION 'payment_methods.gl_account_id is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM accounting_roles
        WHERE role = 'BANK' AND colombia_only = false
    ) THEN
        RAISE EXCEPTION 'generic BANK role is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM accounting_roles
        WHERE role = 'CESANTIAS_PAYABLE' AND colombia_only = true
    ) THEN
        RAISE EXCEPTION 'Colombia payroll roles are missing';
    END IF;
END $$;

ROLLBACK;
