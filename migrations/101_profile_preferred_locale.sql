ALTER TABLE profile
    ADD COLUMN IF NOT EXISTS preferred_locale TEXT;

ALTER TABLE profile
    DROP CONSTRAINT IF EXISTS profile_preferred_locale_supported;

ALTER TABLE profile
    ADD CONSTRAINT profile_preferred_locale_supported
    CHECK (preferred_locale IN ('es', 'en', 'pt', 'fr', 'de', 'ar', 'hi', 'zh'));

COMMENT ON COLUMN profile.preferred_locale IS
    'Personal frontend UI language. NULL preserves the tenant/default fallback.';
