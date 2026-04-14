-- Add is_bar flag to tables — identifies the permanent bar/counter session
-- Issue: https://github.com/uno0uno/warocol.com/issues/338
ALTER TABLE tables ADD COLUMN IF NOT EXISTS is_bar BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for fast lookup of bar table per tenant
CREATE INDEX IF NOT EXISTS idx_tables_is_bar ON tables (tenant_id, is_bar) WHERE is_bar IS TRUE;
