-- Migration 060: per-tenant kill switch for the new RBAC layer
-- Issue #163 — Epic 1 / Sub-task #E1.2
--
-- Adds a tenant-level mode that controls how the upcoming `require_module()`
-- dependency (Epic 2) reacts when a member tries to use a module their role
-- does not grant. The default 'disabled' is the safe choice: every existing
-- tenant lands here on deploy, so module gating is a strict no-op until
-- explicitly enabled per tenant.
--
-- Modes:
--   'disabled' : module gating is off (current behaviour preserved exactly).
--   'shadow'   : decisions are computed and logged, but never enforced.
--                Use to validate the matrix on a real tenant before flipping.
--   'enforce'  : module gating returns 403 when the role lacks the module.
--
-- Activation flow once the system is wired (Epic 2+):
--     UPDATE tenants SET permissions_enforcement_mode = 'shadow'
--      WHERE slug = '<slug>';
--   ... observe shadow logs for a week, fix any over-broad/over-narrow rule,
--   then:
--     UPDATE tenants SET permissions_enforcement_mode = 'enforce'
--      WHERE slug = '<slug>';

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS permissions_enforcement_mode TEXT NOT NULL
        DEFAULT 'disabled'
        CHECK (permissions_enforcement_mode IN ('disabled', 'shadow', 'enforce'));

COMMENT ON COLUMN tenants.permissions_enforcement_mode IS
    'RBAC kill switch per tenant. disabled = no gating (default, safe), shadow = log only, enforce = return 403 on unauthorised module access. Set via SQL during onboarding/audits — no UI yet.';
