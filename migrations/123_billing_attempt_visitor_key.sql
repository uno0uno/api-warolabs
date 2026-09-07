-- Issue #921: persist attribution to payment attempt (additive only)
ALTER TABLE billing_payment_attempts
    ADD COLUMN IF NOT EXISTS visitor_key TEXT;

ALTER TABLE billing_payment_attempts
    ADD COLUMN IF NOT EXISTS lead_id UUID REFERENCES leads(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_billing_attempts_visitor_key
    ON billing_payment_attempts (visitor_key)
    WHERE visitor_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_billing_attempts_lead_id
    ON billing_payment_attempts (lead_id)
    WHERE lead_id IS NOT NULL;

COMMENT ON COLUMN billing_payment_attempts.visitor_key IS
    'Opaque first-party visitor key (waro_visitor_key) copied best-effort from last lead_interactions for this tenant owner; nullable.';
COMMENT ON COLUMN billing_payment_attempts.lead_id IS
    'Origin lead that produced this tenant checkout; nullable, FK to leads(id) SET NULL.';
