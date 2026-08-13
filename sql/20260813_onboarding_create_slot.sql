-- api-warolabs#839: one in-progress onboarding per owner (Starter counts).
-- Crear frees the slot by closing starter_active/active before insert.
DROP INDEX IF EXISTS tenant_onboarding_owner_in_progress_unique;

CREATE UNIQUE INDEX tenant_onboarding_owner_in_progress_unique
    ON tenant_onboarding (owner_user_id)
    WHERE state <> ALL (ARRAY['setup_complete'::text, 'cancelled'::text]);
