-- Run against a disposable database migrated through 107.
\set ON_ERROR_STOP on

BEGIN;

CREATE TEMP TABLE plans_before AS
SELECT to_jsonb(plan) AS row_data
FROM subscription_plans AS plan;

CREATE TEMP TABLE subscriptions_before AS
SELECT to_jsonb(subscription) AS row_data
FROM tenant_subscriptions AS subscription;

\ir ../../migrations/108_onboarding_payment_attempts.sql
\ir ../../migrations/108_onboarding_payment_attempts.sql

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'billing_payment_attempts'
    ) THEN
        RAISE EXCEPTION 'billing_payment_attempts is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'billing_payment_attempts_transaction_unique'
          AND indexdef LIKE '%WHERE (provider_transaction_id IS NOT NULL)%'
    ) THEN
        RAISE EXCEPTION 'provider transaction partial unique index is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'billing_payment_attempts_amount_check'
          AND contype = 'c'
    ) THEN
        RAISE EXCEPTION 'positive amount constraint is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'billing_payment_attempts_reference_unique'
          AND contype = 'u'
    ) THEN
        RAISE EXCEPTION 'provider reference uniqueness is missing';
    END IF;

    IF EXISTS (
        (SELECT row_data FROM plans_before
         EXCEPT SELECT to_jsonb(plan) FROM subscription_plans AS plan)
        UNION ALL
        (SELECT to_jsonb(plan) FROM subscription_plans AS plan
         EXCEPT SELECT row_data FROM plans_before)
    ) THEN
        RAISE EXCEPTION 'migration changed plans or prices';
    END IF;

    IF EXISTS (
        (SELECT row_data FROM subscriptions_before
         EXCEPT SELECT to_jsonb(subscription) FROM tenant_subscriptions AS subscription)
        UNION ALL
        (SELECT to_jsonb(subscription) FROM tenant_subscriptions AS subscription
         EXCEPT SELECT row_data FROM subscriptions_before)
    ) THEN
        RAISE EXCEPTION 'migration changed existing subscriptions';
    END IF;
END $$;

ROLLBACK;
