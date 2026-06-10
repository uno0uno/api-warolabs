-- Migration 084: add payment_method_id to credit_payments (api-warolabs#410)
-- Stores the selected tenant payment sub-method while keeping payment_method
-- as the parent group slug for backward compatibility.

ALTER TABLE credit_payments
  ADD COLUMN IF NOT EXISTS payment_method_id uuid REFERENCES payment_methods(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_credit_payments_payment_method_id
  ON credit_payments(payment_method_id)
  WHERE payment_method_id IS NOT NULL;
