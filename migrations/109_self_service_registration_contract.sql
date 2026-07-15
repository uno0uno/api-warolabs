-- Issue #636: explicit login/registration intent, opaque registration links,
-- durable attribution and idempotent self-service lead notification claims.

ALTER TABLE magic_tokens
    ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'login';

ALTER TABLE onboarding_email_challenges
    ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'registration',
    ADD COLUMN IF NOT EXISTS opaque_token_hash text,
    ADD COLUMN IF NOT EXISTS phone_country_code integer,
    ADD COLUMN IF NOT EXISTS phone_number varchar(20),
    ADD COLUMN IF NOT EXISTS consent_at timestamptz,
    ADD COLUMN IF NOT EXISTS consent_version text,
    ADD COLUMN IF NOT EXISTS first_source varchar(100),
    ADD COLUMN IF NOT EXISTS first_content varchar(100),
    ADD COLUMN IF NOT EXISTS first_campaign varchar(100),
    ADD COLUMN IF NOT EXISTS first_variant varchar(100),
    ADD COLUMN IF NOT EXISTS last_source varchar(100),
    ADD COLUMN IF NOT EXISTS last_content varchar(100),
    ADD COLUMN IF NOT EXISTS last_campaign varchar(100),
    ADD COLUMN IF NOT EXISTS last_variant varchar(100);

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'magic_tokens_purpose_check') THEN
        ALTER TABLE magic_tokens
            ADD CONSTRAINT magic_tokens_purpose_check CHECK (purpose IN ('login', 'registration'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'onboarding_challenge_purpose_check') THEN
        ALTER TABLE onboarding_email_challenges
            ADD CONSTRAINT onboarding_challenge_purpose_check CHECK (purpose IN ('login', 'registration'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'onboarding_challenge_phone_country_check') THEN
        ALTER TABLE onboarding_email_challenges
            ADD CONSTRAINT onboarding_challenge_phone_country_check
            CHECK (phone_country_code IS NULL OR phone_country_code BETWEEN 1 AND 999);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'onboarding_challenge_phone_check') THEN
        ALTER TABLE onboarding_email_challenges
            ADD CONSTRAINT onboarding_challenge_phone_check
            CHECK (phone_number IS NULL OR phone_number ~ '^[0-9]{7,15}$');
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS onboarding_challenge_opaque_token_unique
    ON onboarding_email_challenges (opaque_token_hash)
    WHERE opaque_token_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS onboarding_challenge_retention_created_idx
    ON onboarding_email_challenges (created_at);

CREATE UNIQUE INDEX IF NOT EXISTS leads_self_service_tenant_unique
    ON leads (tenant_id)
    WHERE source = 'self_service_registration';

CREATE UNIQUE INDEX IF NOT EXISTS lead_interactions_registration_verified_unique
    ON lead_interactions (lead_id, interaction_type)
    WHERE interaction_type = 'registration_verified';

COMMENT ON COLUMN magic_tokens.purpose IS
    'Explicit authentication intent. Existing identity magic tokens are login-only.';
COMMENT ON COLUMN onboarding_email_challenges.opaque_token_hash IS
    'Token-only HMAC lookup for registration links that do not expose email.';
COMMENT ON COLUMN leads.tenant_id IS
    'Optional tenant binding. Required by self-service registration leads.';
