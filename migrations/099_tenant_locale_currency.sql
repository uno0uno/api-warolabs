-- Tenant receipt localization defaults.
-- Backend receipt emails resolve language/currency from tenant_public_profiles,
-- not from frontend cashier locale.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS locale TEXT NOT NULL DEFAULT 'es';

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'COP';

UPDATE tenant_public_profiles
SET locale = 'es'
WHERE locale IS NULL OR btrim(locale) = '' OR lower(locale) NOT IN ('es', 'en');

UPDATE tenant_public_profiles
SET currency_code = 'COP'
WHERE currency_code IS NULL OR btrim(currency_code) = '' OR upper(currency_code) NOT IN ('COP');

UPDATE tenant_public_profiles
SET locale = lower(locale),
    currency_code = upper(currency_code);

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_locale_supported;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_locale_supported
    CHECK (locale IN ('es', 'en'));

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_currency_code_supported;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_currency_code_supported
    CHECK (currency_code IN ('COP'));

COMMENT ON COLUMN tenant_public_profiles.locale IS
    'Tenant-facing locale for backend-rendered receipts and emails. Initial values: es, en.';

COMMENT ON COLUMN tenant_public_profiles.currency_code IS
    'Tenant currency for backend-rendered receipts. Initial supported value: COP.';
