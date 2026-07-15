-- Issue #637: explicit self-service trial lifecycle, separate from paid grace.

ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS trial_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS trial_ends_at timestamptz;

ALTER TABLE tenant_subscriptions
    DROP CONSTRAINT IF EXISTS tenant_subscriptions_status_check;

ALTER TABLE tenant_subscriptions
    ADD CONSTRAINT tenant_subscriptions_status_check CHECK (status IN (
        'pending',
        'trialing',
        'trial_expired',
        'active',
        'past_due',
        'cancelled',
        'expired'
    ));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_subscriptions_trial_window_check'
    ) THEN
        ALTER TABLE tenant_subscriptions
            ADD CONSTRAINT tenant_subscriptions_trial_window_check CHECK (
                (trial_started_at IS NULL AND trial_ends_at IS NULL)
                OR (
                    trial_started_at IS NOT NULL
                    AND trial_ends_at = trial_started_at + INTERVAL '15 days'
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_subscriptions_trial_state_check'
    ) THEN
        ALTER TABLE tenant_subscriptions
            ADD CONSTRAINT tenant_subscriptions_trial_state_check CHECK (
                status NOT IN ('trialing', 'trial_expired')
                OR (trial_started_at IS NOT NULL AND trial_ends_at IS NOT NULL)
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_trial_due
    ON tenant_subscriptions (trial_ends_at)
    WHERE status = 'trialing';

CREATE UNIQUE INDEX IF NOT EXISTS billing_events_trial_once
    ON billing_events (subscription_id, event_type)
    WHERE event_type IN (
        'trial_started',
        'trial_warning_day_7',
        'trial_warning_day_3',
        'trial_warning_day_1',
        'trial_expired'
    );

CREATE TABLE IF NOT EXISTS trial_warning_deliveries (
    subscription_id UUID NOT NULL REFERENCES tenant_subscriptions(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    days_remaining SMALLINT NOT NULL CHECK (days_remaining IN (7, 3, 1)),
    trial_ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'sending'
        CHECK (status IN ('sending', 'pending', 'sent')),
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    claimed_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (subscription_id, days_remaining)
);

COMMENT ON TABLE trial_warning_deliveries IS
    'PII-free durable delivery claims for trial warning email idempotency.';

COMMENT ON COLUMN tenant_subscriptions.trial_started_at IS
    'Database-authoritative start of the tenant self-service trial.';
COMMENT ON COLUMN tenant_subscriptions.trial_ends_at IS
    'Exact trial access boundary: trial_started_at plus 15 days.';

-- Rollback (safe only after confirming no trialing/trial_expired rows exist):
-- DROP TABLE IF EXISTS trial_warning_deliveries;
-- DROP INDEX IF EXISTS billing_events_trial_once;
-- DROP INDEX IF EXISTS idx_tenant_subscriptions_trial_due;
-- ALTER TABLE tenant_subscriptions DROP CONSTRAINT IF EXISTS tenant_subscriptions_trial_state_check;
-- ALTER TABLE tenant_subscriptions DROP CONSTRAINT IF EXISTS tenant_subscriptions_trial_window_check;
-- ALTER TABLE tenant_subscriptions DROP CONSTRAINT IF EXISTS tenant_subscriptions_status_check;
-- ALTER TABLE tenant_subscriptions ADD CONSTRAINT tenant_subscriptions_status_check
--   CHECK (status IN ('pending','active','past_due','cancelled','expired'));
-- ALTER TABLE tenant_subscriptions DROP COLUMN IF EXISTS trial_ends_at;
-- ALTER TABLE tenant_subscriptions DROP COLUMN IF EXISTS trial_started_at;
