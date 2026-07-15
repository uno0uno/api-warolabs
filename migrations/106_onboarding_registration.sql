-- Issue #630: resumable self-service registration without operational access.

CREATE UNIQUE INDEX IF NOT EXISTS profile_email_lower_unique
    ON profile (lower(trim(email)));

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS lifecycle_status text NOT NULL DEFAULT 'active';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tenants_lifecycle_status_check'
    ) THEN
        ALTER TABLE tenants
            ADD CONSTRAINT tenants_lifecycle_status_check
            CHECK (lifecycle_status IN ('pending', 'active', 'suspended', 'cancelled'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS onboarding_email_challenges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    normalized_email text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    code_hash text NOT NULL,
    request_ip inet,
    user_agent text,
    failed_attempts integer NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    completed_user_id uuid REFERENCES profile(id),
    completed_tenant_id uuid REFERENCES tenants(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT onboarding_email_normalized_check
        CHECK (normalized_email = lower(trim(normalized_email))),
    CONSTRAINT onboarding_email_attempts_check
        CHECK (failed_attempts BETWEEN 0 AND 5)
);

CREATE INDEX IF NOT EXISTS idx_onboarding_challenge_email_created
    ON onboarding_email_challenges (normalized_email, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_onboarding_challenge_ip_created
    ON onboarding_email_challenges (request_ip, created_at DESC)
    WHERE request_ip IS NOT NULL;

CREATE TABLE IF NOT EXISTS tenant_onboarding (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL UNIQUE REFERENCES tenants(id),
    owner_user_id uuid NOT NULL REFERENCES profile(id),
    verified_email text NOT NULL,
    state text NOT NULL DEFAULT 'business_profile_pending',
    email_verified_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_onboarding_email_normalized_check
        CHECK (verified_email = lower(trim(verified_email))),
    CONSTRAINT tenant_onboarding_state_check CHECK (state IN (
        'email_verified',
        'business_profile_pending',
        'terms_pending',
        'payment_pending',
        'paid',
        'active',
        'setup_complete',
        'cancelled'
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_onboarding_owner_in_progress_unique
    ON tenant_onboarding (owner_user_id)
    WHERE state NOT IN ('setup_complete', 'cancelled');

CREATE UNIQUE INDEX IF NOT EXISTS tenant_members_pending_owner_unique
    ON tenant_members (tenant_id, user_id)
    WHERE role = 'owner';

COMMENT ON COLUMN tenants.lifecycle_status IS
    'Server-owned tenant lifecycle. Public onboarding creates pending; trusted payment activation promotes to active.';

COMMENT ON TABLE onboarding_email_challenges IS
    'Hashed, rate-limited pre-registration challenges. No profile or tenant is required before verification.';

COMMENT ON TABLE tenant_onboarding IS
    'Server-side onboarding state machine and owner binding for a tenant.';
