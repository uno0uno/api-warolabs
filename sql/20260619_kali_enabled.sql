-- api-warolabs#483 — internal tenant gate for Kali.
--
-- Kali is controlled internally only. Do not expose this flag through tenant
-- UI, operaciones toggles, or public administration endpoints.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS kali_enabled BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.kali_enabled IS
    'Internal-only feature flag for Kali AI assistant availability. No UI toggle; default false.';

-- First release: enable Kali only for WARO Colombia.
INSERT INTO tenant_public_profiles (tenant_id, slug, display_name, kali_enabled)
SELECT t.id, t.slug, t.name, true
FROM tenants t
WHERE t.slug = 'warocolombia'
ON CONFLICT (tenant_id) DO UPDATE
    SET kali_enabled = true,
        updated_at = now();
