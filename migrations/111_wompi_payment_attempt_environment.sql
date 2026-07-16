-- Issue #643: isolate Wompi Production and Sandbox payment evidence.

ALTER TABLE billing_payment_attempts
    ADD COLUMN IF NOT EXISTS provider_environment text;

UPDATE billing_payment_attempts
SET provider_environment = 'prod'
WHERE provider_environment IS NULL;

ALTER TABLE billing_payment_attempts
    ALTER COLUMN provider_environment SET DEFAULT 'prod',
    ALTER COLUMN provider_environment SET NOT NULL;

ALTER TABLE billing_payment_attempts
    DROP CONSTRAINT IF EXISTS billing_payment_attempts_environment_check;

ALTER TABLE billing_payment_attempts
    ADD CONSTRAINT billing_payment_attempts_environment_check
    CHECK (provider_environment IN ('prod', 'test'));

ALTER TABLE billing_payment_attempts
    DROP CONSTRAINT IF EXISTS billing_payment_attempts_reference_unique,
    DROP CONSTRAINT IF EXISTS billing_payment_attempts_environment_reference_unique;

ALTER TABLE billing_payment_attempts
    ADD CONSTRAINT billing_payment_attempts_environment_reference_unique
    UNIQUE (provider_environment, provider_reference);

DROP INDEX IF EXISTS billing_payment_attempts_transaction_unique;

CREATE UNIQUE INDEX billing_payment_attempts_transaction_unique
    ON billing_payment_attempts (provider_environment, provider_transaction_id)
    WHERE provider_transaction_id IS NOT NULL;

COMMENT ON COLUMN billing_payment_attempts.provider_environment IS
    'Signed Wompi event environment: prod for real payments, test for Sandbox.';
