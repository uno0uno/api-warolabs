-- 070_add_waiter_attribution_enabled_to_tenant_public_profiles.sql
-- Issue warocol.com#573 — feat(operaciones): asignar mesero por mesa para trazabilidad
--
-- Adds a per-tenant boolean flag that controls visibility of the waiter
-- attribution UI family: admin panel in /operaciones/comandas (#573),
-- POS mesa override (warocol.com#574), and bar/counter order attribution
-- (warocol.com#575).
--
-- Default false to preserve current behavior. Independent of
-- tables_enabled — the bar/counter flow (#575) works without tables.

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS waiter_attribution_enabled BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.waiter_attribution_enabled IS
    'Waiter attribution feature flag (warocol.com#573). When true, surfaces '
    'the "Mesero por mesa" admin panel + POS UX for assigning members to '
    'tables, sessions, and orders. Independent of tables_enabled — '
    'bar/counter modes (#575) work without tables.';
