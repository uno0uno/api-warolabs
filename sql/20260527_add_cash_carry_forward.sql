-- warocol.com#922 — carry-forward opening float on close + tenant default

ALTER TABLE closing_summary
    ADD COLUMN IF NOT EXISTS cash_left_in_drawer numeric(14,2) NULL;

COMMENT ON COLUMN closing_summary.cash_left_in_drawer IS
    'Cash declared left in drawer for the next shift after Z close (#922).';

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS default_opening_cash numeric(14,2) NULL;

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_default_opening_cash_check;
ALTER TABLE tenants
    ADD CONSTRAINT tenants_default_opening_cash_check
    CHECK (default_opening_cash IS NULL OR default_opening_cash >= 0);

COMMENT ON COLUMN tenants.default_opening_cash IS
    'Default fondo de caja when no prior close exists (#922).';
