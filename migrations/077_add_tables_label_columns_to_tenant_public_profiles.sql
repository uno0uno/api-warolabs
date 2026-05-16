-- 076_add_tables_label_columns_to_tenant_public_profiles.sql
-- Issue warocol.com#614 — tenant-global persistence of the custom mesa label
-- (follow-up to warocol.com#612 which shipped the v1 as per-device localStorage).
--
-- Both columns NULLable; NULL means "use the frontend default (Mesa / Mesas)".
-- Empty/whitespace input on the API is normalized to NULL so users can reset.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS tables_label_singular VARCHAR(40),
    ADD COLUMN IF NOT EXISTS tables_label_plural   VARCHAR(40);

COMMENT ON COLUMN tenant_public_profiles.tables_label_singular IS
    'Custom singular noun for "Mesa" (warocol.com#614). E.g. "Habitación" for hotels. NULL → frontend uses "Mesa".';
COMMENT ON COLUMN tenant_public_profiles.tables_label_plural IS
    'Custom plural noun for "Mesas" (warocol.com#614). E.g. "Habitaciones". NULL → frontend uses "Mesas".';
