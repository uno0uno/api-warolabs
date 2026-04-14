-- Migration 020: add is_discarded to table_sessions
-- Additive only — safe for prod. Never DROPs or ALTERs existing columns.
-- Issue: https://github.com/uno0uno/warocol.com/issues/337

ALTER TABLE table_sessions
  ADD COLUMN IF NOT EXISTS is_discarded BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_table_sessions_is_discarded
  ON table_sessions (is_discarded)
  WHERE is_discarded = TRUE;
