-- api-warolabs#411/#410 dependency - persist selected custom payment method for credit payments.

ALTER TABLE credit_payments
    ADD COLUMN IF NOT EXISTS payment_method_id UUID REFERENCES payment_methods(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_credit_payments_payment_method_id
    ON credit_payments (payment_method_id)
    WHERE payment_method_id IS NOT NULL;
