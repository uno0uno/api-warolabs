-- Migration 015: relax chk_payment_method constraint (issue #343)
-- The old constraint hardcoded legacy slugs (transfer, check, cash, credit_card, debit_card, other).
-- Payment methods are now dynamic (payment_method_groups table), so the constraint
-- must allow any non-empty short string. No data is removed or modified.

ALTER TABLE tenant_purchases DROP CONSTRAINT IF EXISTS chk_payment_method;
