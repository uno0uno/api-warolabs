-- Issue #795: allow Paddle onboarding payment attempts (USD/EUR + provider=paddle).
-- Additive only: widen CHECKs; existing Wompi/COP rows remain valid.

ALTER TABLE billing_payment_attempts
    DROP CONSTRAINT IF EXISTS billing_payment_attempts_provider_check;

ALTER TABLE billing_payment_attempts
    ADD CONSTRAINT billing_payment_attempts_provider_check
    CHECK (provider IN ('wompi', 'paddle'));

ALTER TABLE billing_payment_attempts
    DROP CONSTRAINT IF EXISTS billing_payment_attempts_currency_check;

ALTER TABLE billing_payment_attempts
    ADD CONSTRAINT billing_payment_attempts_currency_check
    CHECK (currency IN ('COP', 'USD', 'EUR'));

COMMENT ON TABLE billing_payment_attempts IS
    'Append-only checkout evidence (Wompi or Paddle) used to activate paid onboarding from verified webhooks.';

COMMENT ON COLUMN billing_payment_attempts.expected_amount_in_cents IS
    'Server-calculated annual price in provider minor units (COP centavos or USD/EUR cents).';
