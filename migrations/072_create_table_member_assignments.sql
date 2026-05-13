-- 072_create_table_member_assignments.sql
-- Issue warocol.com#573 — append-only history of waiter assignments per table.
--
-- Period model: each row represents one period a specific member was the
-- assigned default for a table. `assigned_at` is always set; `unassigned_at`
-- becomes set when the assignment changes (closing the previous period).
-- The latest open row (unassigned_at IS NULL) is the current default —
-- mirrored fast in `tables.assigned_member_id` (migration 071).
--
-- Snapshots of member_name + member_role preserve auditability even if
-- the member is deleted from tenant_members later.

CREATE TABLE IF NOT EXISTS table_member_assignments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    table_id      UUID NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    member_id     UUID NULL REFERENCES tenant_members(id) ON DELETE SET NULL,
    member_name   VARCHAR(200),
    member_role   VARCHAR(50),
    assigned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    unassigned_at TIMESTAMPTZ NULL,
    assigned_by   UUID NULL REFERENCES profile(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tma_table_history
    ON table_member_assignments(table_id, assigned_at DESC);

-- Partial index: fast lookup of current assignment per table
CREATE INDEX IF NOT EXISTS idx_tma_current_per_table
    ON table_member_assignments(table_id) WHERE unassigned_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tma_tenant
    ON table_member_assignments(tenant_id);

COMMENT ON TABLE table_member_assignments IS
    'Append-only history of default waiter assignments per table '
    '(warocol.com#573). Period model: each row spans one continuous '
    'assignment, with snapshots that survive member deletion.';
