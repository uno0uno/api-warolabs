-- 095_tenant_timezone.sql
-- api-warolabs#530 - tenant operational timezone contract.
--
-- Defaults preserve existing Colombia behavior while allowing future tenants
-- to declare an IANA timezone for operational dates and business hours.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'America/Bogota';

UPDATE tenant_public_profiles
SET timezone = 'America/Bogota'
WHERE timezone IS NULL OR btrim(timezone) = '';

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_timezone_non_blank;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_timezone_non_blank
    CHECK (btrim(timezone) <> '');

COMMENT ON COLUMN tenant_public_profiles.timezone IS
    'IANA timezone for tenant operational dates, business hours, and report boundaries. Defaults to America/Bogota.';
