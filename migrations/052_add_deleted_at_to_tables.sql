-- Migration 052: add deleted_at to tables
-- Distinguishes permanently deleted (deleted_at IS NOT NULL) from
-- temporarily deactivated (is_active = false, deleted_at IS NULL).
-- Issue: https://github.com/uno0uno/warocol.com/issues/436

ALTER TABLE tables ADD COLUMN deleted_at TIMESTAMPTZ DEFAULT NULL;
