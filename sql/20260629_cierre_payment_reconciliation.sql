-- Payment method reconciliation for cierre/arqueo.
-- Additive migration: keeps legacy cierre_payment_breakdown.total intact.

ALTER TABLE cierre_payment_breakdown
    ADD COLUMN IF NOT EXISTS expected_amount numeric(12,2),
    ADD COLUMN IF NOT EXISTS reported_amount numeric(12,2),
    ADD COLUMN IF NOT EXISTS difference_amount numeric(12,2),
    ADD COLUMN IF NOT EXISTS reconciliation_status varchar(30),
    ADD COLUMN IF NOT EXISTS reconciliation_reason varchar(50),
    ADD COLUMN IF NOT EXISTS reconciliation_notes text,
    ADD COLUMN IF NOT EXISTS journal_entry_id uuid,
    ADD COLUMN IF NOT EXISTS resolved_by uuid,
    ADD COLUMN IF NOT EXISTS resolved_at timestamptz;

UPDATE cierre_payment_breakdown
SET expected_amount = total
WHERE expected_amount IS NULL;

UPDATE cierre_payment_breakdown
SET reconciliation_status = CASE
    WHEN group_slug IN ('cash', 'untracked') OR COALESCE(total, 0) <= 0 THEN 'not_required'
    ELSE 'pending'
END
WHERE reconciliation_status IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'cierre_payment_breakdown_journal_entry_id_fkey'
    ) THEN
        ALTER TABLE cierre_payment_breakdown
            ADD CONSTRAINT cierre_payment_breakdown_journal_entry_id_fkey
            FOREIGN KEY (journal_entry_id)
            REFERENCES tenant_journal_entries(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cierre_payment_breakdown_reconciliation
    ON cierre_payment_breakdown (reconciliation_status, group_slug);

CREATE INDEX IF NOT EXISTS idx_cierre_payment_breakdown_journal_entry
    ON cierre_payment_breakdown (journal_entry_id)
    WHERE journal_entry_id IS NOT NULL;

COMMENT ON COLUMN cierre_payment_breakdown.expected_amount IS
    'System expected amount for this close/payment method; initialized from legacy total.';
COMMENT ON COLUMN cierre_payment_breakdown.reported_amount IS
    'Amount reported from payment provider/operator during reconciliation.';
COMMENT ON COLUMN cierre_payment_breakdown.difference_amount IS
    'reported_amount - expected_amount. NULL until reported_amount is entered.';
COMMENT ON COLUMN cierre_payment_breakdown.reconciliation_status IS
    'not_required, pending, matched, needs_review, or resolved.';
COMMENT ON COLUMN cierre_payment_breakdown.reconciliation_reason IS
    'Resolution reason selected by finance, e.g. timing, commission, missing_sale.';
