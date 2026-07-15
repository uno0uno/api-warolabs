-- Issue #632: immutable Wompi payment attempts for paid onboarding.

CREATE TABLE IF NOT EXISTS billing_payment_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    plan_id uuid NOT NULL REFERENCES subscription_plans(id),
    provider text NOT NULL DEFAULT 'wompi',
    provider_reference text,
    expected_amount_in_cents bigint NOT NULL,
    currency char(3) NOT NULL DEFAULT 'COP',
    billing_cycle text NOT NULL DEFAULT 'annual',
    status text NOT NULL DEFAULT 'created',
    provider_transaction_id text,
    checkout_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    CONSTRAINT billing_payment_attempts_provider_check
        CHECK (provider = 'wompi'),
    CONSTRAINT billing_payment_attempts_reference_unique
        UNIQUE (provider_reference),
    CONSTRAINT billing_payment_attempts_amount_check
        CHECK (expected_amount_in_cents > 0),
    CONSTRAINT billing_payment_attempts_currency_check
        CHECK (currency = 'COP'),
    CONSTRAINT billing_payment_attempts_cycle_check
        CHECK (billing_cycle = 'annual'),
    CONSTRAINT billing_payment_attempts_status_check
        CHECK (status IN ('created', 'pending', 'approved', 'declined', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_billing_payment_attempts_tenant_created
    ON billing_payment_attempts (tenant_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS billing_payment_attempts_transaction_unique
    ON billing_payment_attempts (provider_transaction_id)
    WHERE provider_transaction_id IS NOT NULL;

COMMENT ON TABLE billing_payment_attempts IS
    'Append-only Wompi checkout evidence used to activate paid onboarding from verified webhooks.';

COMMENT ON COLUMN billing_payment_attempts.id IS
    'Opaque checkout SKU; it contains no tenant or email PII.';

COMMENT ON COLUMN billing_payment_attempts.expected_amount_in_cents IS
    'Server-calculated annual plan price in exact COP minor units.';
