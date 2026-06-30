-- Additive migration: method-level outflows for arqueo reconciliation.
-- Keeps historical closing/payment breakdown rows compatible.

ALTER TABLE closing_summary
    ADD COLUMN IF NOT EXISTS cash_purchases numeric(12,2) DEFAULT 0 NOT NULL;

ALTER TABLE cierre_payment_breakdown
    ADD COLUMN IF NOT EXISTS gross_inflows_amount numeric(12,2),
    ADD COLUMN IF NOT EXISTS expense_outflows_amount numeric(12,2) DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS purchase_outflows_amount numeric(12,2) DEFAULT 0 NOT NULL;

UPDATE cierre_payment_breakdown
SET gross_inflows_amount = total
WHERE gross_inflows_amount IS NULL;

UPDATE cierre_payment_breakdown
SET expected_amount = total
WHERE expected_amount IS NULL;

COMMENT ON COLUMN closing_summary.cash_purchases IS
    'Cash paid direct purchases included as physical drawer outflows in arqueo.';

COMMENT ON COLUMN cierre_payment_breakdown.gross_inflows_amount IS
    'Gross inflows by payment method before expense/purchase outflows.';

COMMENT ON COLUMN cierre_payment_breakdown.expense_outflows_amount IS
    'Expense outflows paid through this payment method during the cierre window.';

COMMENT ON COLUMN cierre_payment_breakdown.purchase_outflows_amount IS
    'Direct purchase outflows paid through this payment method during the cierre window.';
