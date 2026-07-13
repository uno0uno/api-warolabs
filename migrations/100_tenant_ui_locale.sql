-- Tenant-wide dashboard language. Kept separate from `locale`, which remains
-- the es/en source for backend-rendered receipts and emails.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS ui_locale TEXT NOT NULL DEFAULT 'es';

UPDATE tenant_public_profiles
SET ui_locale = 'es'
WHERE ui_locale IS NULL
   OR btrim(ui_locale) = ''
   OR lower(ui_locale) NOT IN ('es', 'en', 'pt', 'fr', 'de', 'hi', 'zh', 'ar');

UPDATE tenant_public_profiles
SET ui_locale = lower(ui_locale);

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_ui_locale_supported;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_ui_locale_supported
    CHECK (ui_locale IN ('es', 'en', 'pt', 'fr', 'de', 'hi', 'zh', 'ar'));

COMMENT ON COLUMN tenant_public_profiles.ui_locale IS
    'Tenant-wide frontend UI language. Independent from receipt/email locale.';
