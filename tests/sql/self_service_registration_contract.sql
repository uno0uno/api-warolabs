-- Run against a disposable database with the pre-onboarding application schema.
\set ON_ERROR_STOP on

BEGIN;

\ir ../../migrations/106_onboarding_registration.sql
\ir ../../migrations/109_self_service_registration_contract.sql
\ir ../../migrations/109_self_service_registration_contract.sql

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'magic_tokens' AND column_name = 'purpose'
    ) THEN
        RAISE EXCEPTION 'magic token purpose is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'onboarding_email_challenges'
          AND column_name = 'opaque_token_hash'
    ) THEN
        RAISE EXCEPTION 'opaque challenge lookup is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'leads_self_service_tenant_unique'
          AND indexdef LIKE 'CREATE UNIQUE INDEX%'
          AND position('self_service_registration' in indexdef) > 0
    ) THEN
        RAISE EXCEPTION 'self-service lead uniqueness is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'lead_interactions_registration_verified_unique'
    ) THEN
        RAISE EXCEPTION 'registration event uniqueness is missing';
    END IF;
END $$;

DO $$
DECLARE
    v_profile_id uuid := gen_random_uuid();
    v_tenant_id uuid := gen_random_uuid();
    v_lead_id uuid;
    lead_count integer;
    event_count integer;
    stored_source text;
BEGIN
    INSERT INTO profile (id, email, created_at, updated_at)
    VALUES (v_profile_id, 'wr636-' || v_profile_id || '@example.invalid', now(), now());

    INSERT INTO tenants (id, name, slug, email, lifecycle_status, created_at)
    VALUES (
        v_tenant_id,
        'WR 636 disposable',
        'wr636-' || v_tenant_id,
        'wr636-' || v_tenant_id || '@example.invalid',
        'pending',
        now()
    );

    INSERT INTO leads (
        profile_id, tenant_id, email, phone, source, status,
        utm_source, utm_campaign, utm_content
    )
    VALUES (
        v_profile_id, v_tenant_id, 'wr636@example.invalid', '+573001234567',
        'self_service_registration', 'verified', 'home', 'trial', 'hero'
    )
    ON CONFLICT (tenant_id) WHERE source = 'self_service_registration'
    DO UPDATE SET status = 'verified'
    RETURNING id INTO v_lead_id;

    INSERT INTO leads (
        profile_id, tenant_id, email, source, status, utm_source
    )
    VALUES (
        v_profile_id, v_tenant_id, 'wr636@example.invalid',
        'self_service_registration', 'verified', 'blog'
    )
    ON CONFLICT (tenant_id) WHERE source = 'self_service_registration'
    DO UPDATE SET status = 'verified';

    SELECT count(*), min(utm_source)
    INTO lead_count, stored_source
    FROM leads
    WHERE leads.tenant_id = v_tenant_id
      AND source = 'self_service_registration';
    IF lead_count <> 1 OR stored_source <> 'home' THEN
        RAISE EXCEPTION 'lead idempotency or immutable first-touch failed';
    END IF;

    INSERT INTO lead_interactions (lead_id, interaction_type, source)
    VALUES (v_lead_id, 'registration_verified', 'home')
    ON CONFLICT (lead_id, interaction_type)
        WHERE interaction_type = 'registration_verified'
    DO NOTHING;
    INSERT INTO lead_interactions (lead_id, interaction_type, source)
    VALUES (v_lead_id, 'registration_verified', 'blog')
    ON CONFLICT (lead_id, interaction_type)
        WHERE interaction_type = 'registration_verified'
    DO NOTHING;

    SELECT count(*) INTO event_count
    FROM lead_interactions
    WHERE lead_interactions.lead_id = v_lead_id
      AND interaction_type = 'registration_verified';
    IF event_count <> 1 THEN
        RAISE EXCEPTION 'registration event idempotency failed';
    END IF;
END $$;

ROLLBACK;
