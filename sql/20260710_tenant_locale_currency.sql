-- Tenant locale + display currency prefs (warocol.com#1599 / epic #1598 B1).
-- Additive only. Defaults preserve Colombia product feel (es + COP).

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS locale TEXT NOT NULL DEFAULT 'es';

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'COP';

UPDATE tenant_public_profiles
SET locale = 'es'
WHERE locale IS NULL OR btrim(locale) = '';

UPDATE tenant_public_profiles
SET currency_code = 'COP'
WHERE currency_code IS NULL OR btrim(currency_code) = '';

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_locale_allowed;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_locale_allowed
    CHECK (locale IN ('es', 'en'));

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_currency_code_non_blank;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_currency_code_non_blank
    CHECK (btrim(currency_code) <> '' AND char_length(btrim(currency_code)) = 3);

COMMENT ON COLUMN tenant_public_profiles.locale IS
    'UI/number language preference: es | en. Defaults to es (Colombia product).';

COMMENT ON COLUMN tenant_public_profiles.currency_code IS
    'ISO 4217 display currency code. Defaults to COP. Display-only; no FX.';
