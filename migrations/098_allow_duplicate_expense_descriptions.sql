-- Allow multiple expenses with the same description/name in the same category and month.
-- The expense description is not a unique identity field.

ALTER TABLE tenant_expenses
    DROP CONSTRAINT IF EXISTS tenant_expenses_tenant_id_expense_category_id_month_year_de_key;

DROP INDEX IF EXISTS tenant_expenses_tenant_id_expense_category_id_month_year_de_key;
