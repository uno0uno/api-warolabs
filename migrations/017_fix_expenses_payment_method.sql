-- Migration 017: fix payment_method column in tenant_expenses (issue #343)
-- Widens varchar(20) → varchar(50) to hold dynamic group slugs
-- and adds payment_method_id uuid FK for sub-method identity.

ALTER TABLE tenant_expenses
  ALTER COLUMN payment_method TYPE character varying(50);

ALTER TABLE tenant_expenses
  ADD COLUMN IF NOT EXISTS payment_method_id uuid REFERENCES payment_methods(id) ON DELETE SET NULL;
