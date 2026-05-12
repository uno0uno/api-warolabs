-- 071_add_assigned_member_id_to_tables.sql
-- Issue warocol.com#573 — default waiter assignment per table.
--
-- Adds a nullable FK pointer from tables to tenant_members. The actual
-- audit trail of who-was-assigned-when lives in table_member_assignments
-- (migration 072) with denormalized snapshots; this column is the fast
-- "current default" lookup.
--
-- ON DELETE SET NULL preserves the table row if the member is removed;
-- the history table preserves the audit via snapshots.

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS assigned_member_id UUID NULL
    REFERENCES tenant_members(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tables_assigned_member
    ON tables(assigned_member_id)
    WHERE assigned_member_id IS NOT NULL;

COMMENT ON COLUMN tables.assigned_member_id IS
    'Default waiter assigned to this table (warocol.com#573). NULL = no '
    'default. Bars (is_bar = true) should never have this set (enforced '
    'in application code, not DB constraint). Full history with snapshots '
    'lives in table_member_assignments.';
