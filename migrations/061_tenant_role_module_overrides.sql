-- Migration 061: per-tenant overrides on the role-module access matrix
-- Issue #163 — Epic 1 / Sub-task #E1.3
--
-- Stores deltas applied on top of `DEFAULT_ROLE_MODULES` (defined in
-- app/core/permissions.py). Empty rows for a (tenant, role) pair => the
-- defaults apply unchanged. A row with `granted = true` adds a module the
-- defaults do not include; `granted = false` removes a module the defaults
-- do include. Merge semantics live in the Python resolver (Epic 1 / #E1.4).
--
-- Naming note: the new table is `tenant_role_module_overrides` (not
-- `tenant_role_modules`) to avoid confusion with the pre-existing legacy
-- `tenant_member_roles` table — that one belongs to a previous, orthogonal
-- RBAC attempt scoped per-`site` and is intentionally left untouched here.
-- See issue #163 comment "Legacy State Snapshot" for full context.
--
-- The audit table records every insert/update/delete with the actor session
-- so admins can trace why a tenant's matrix differs from defaults. Inserts
-- happen application-side (cleaner attribution + simpler than triggers in
-- a repo without a defensive-trigger pattern yet).

CREATE TABLE IF NOT EXISTS tenant_role_module_overrides (
    tenant_id  UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role       TEXT        NOT NULL,
    module     TEXT        NOT NULL,
    granted    BOOLEAN     NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, role, module)
);

COMMENT ON TABLE tenant_role_module_overrides IS
    'Per-tenant deltas on top of DEFAULT_ROLE_MODULES (app/core/permissions.py). Empty rows => defaults apply. granted=true adds, granted=false removes. Merge happens in the Python resolver.';

CREATE INDEX IF NOT EXISTS idx_tenant_role_module_overrides_tenant
    ON tenant_role_module_overrides(tenant_id);


CREATE TABLE IF NOT EXISTS tenant_role_module_overrides_audit (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID        NOT NULL,
    role              TEXT        NOT NULL,
    module            TEXT        NOT NULL,
    action            TEXT        NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
    old_granted       BOOLEAN,
    new_granted       BOOLEAN,
    actor_user_id     UUID,
    actor_session_id  UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE tenant_role_module_overrides_audit IS
    'Append-only history of changes to tenant_role_module_overrides. Written application-side from the admin endpoint that mutates the override table (Epic 4 / #E4.x).';

CREATE INDEX IF NOT EXISTS idx_tenant_role_module_overrides_audit_tenant_created
    ON tenant_role_module_overrides_audit(tenant_id, created_at DESC);
