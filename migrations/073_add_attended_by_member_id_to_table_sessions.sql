-- 073_add_attended_by_member_id_to_table_sessions.sql
-- Issue warocol.com#574 — POS session-level waiter override + auto-handoff.
--
-- Adds a nullable FK from table_sessions to tenant_members for the
-- "who is currently attending THIS session" override. The default
-- (tables.assigned_member_id from migration 071/issue #573) is used
-- when this column is NULL, resolved via COALESCE in app code.
--
-- ON DELETE SET NULL preserves the session if the member is removed.

ALTER TABLE table_sessions
    ADD COLUMN IF NOT EXISTS attended_by_member_id UUID NULL
    REFERENCES tenant_members(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_table_sessions_attended_by
    ON table_sessions(attended_by_member_id)
    WHERE attended_by_member_id IS NOT NULL;

COMMENT ON COLUMN table_sessions.attended_by_member_id IS
    'Override of the default waiter for this specific session '
    '(warocol.com#574). NULL = inherit from tables.assigned_member_id. '
    'Set at open via POST body or changed mid-session via PATCH '
    '/pos/tables/{id}/session-waiter. Auto-handoff rule enforced in '
    'app code: only the current waiter or supervisor+ can reassign.';
