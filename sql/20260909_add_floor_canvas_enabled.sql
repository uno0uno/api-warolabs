-- uno0uno/warocol.com#2623 — floor canvas feature flag per tenant.
-- Additive: nullable-safe boolean with default false (apagado = todo como hoy).

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS floor_canvas_enabled boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.floor_canvas_enabled IS
    'When true, POS shows the free floor-plan canvas instead of the zone matrix; Operaciones/Mesas shows the canvas editor. Default false preserves current behaviour.';
