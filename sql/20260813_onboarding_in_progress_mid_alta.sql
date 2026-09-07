-- api-warolabs#836: only one mid-alta onboarding per owner.
-- starter_active / active no longer block creating another business.
DROP INDEX IF EXISTS tenant_onboarding_owner_in_progress_unique;

CREATE UNIQUE INDEX tenant_onboarding_owner_in_progress_unique
    ON tenant_onboarding (owner_user_id)
    WHERE state IN (
        'email_verified',
        'business_profile_pending',
        'terms_pending',
        'payment_pending',
        'paid'
    );
