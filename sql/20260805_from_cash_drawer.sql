-- api-warolabs#786 / epic #785: till cash vs other cash for arqueo
-- ADD ONLY — do not DROP or rewrite existing columns.

ALTER TABLE tenant_expenses
    ADD COLUMN IF NOT EXISTS from_cash_drawer boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN tenant_expenses.from_cash_drawer IS
    'When payment is cash: true = left the till (counts in arqueo); false = other cash source (excluded from drawer expected). Default true preserves legacy behavior.';

ALTER TABLE tenant_purchases
    ADD COLUMN IF NOT EXISTS from_cash_drawer boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN tenant_purchases.from_cash_drawer IS
    'When payment is cash: true = left the till (counts in arqueo); false = other cash source (excluded from drawer expected). Default true preserves legacy behavior.';
