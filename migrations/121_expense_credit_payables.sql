-- Migration 121: gastos a crédito payables (#2113 / epic #2109)
-- ADD-only: payment_type + paid_at for credit expenses in Pagos hub.

ALTER TABLE tenant_expenses
    ADD COLUMN IF NOT EXISTS payment_type VARCHAR(20) NOT NULL DEFAULT 'contado';

ALTER TABLE tenant_expenses
    ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ NULL;

-- Existing expenses were paid at create (contado with method).
UPDATE tenant_expenses
SET paid_at = COALESCE(paid_at, created_at)
WHERE paid_at IS NULL
  AND lower(COALESCE(payment_type, 'contado')) = 'contado';

CREATE INDEX IF NOT EXISTS idx_tenant_expenses_credit_payables
    ON tenant_expenses (tenant_id, payment_type, paid_at)
    WHERE lower(payment_type) = 'credito' AND paid_at IS NULL;
