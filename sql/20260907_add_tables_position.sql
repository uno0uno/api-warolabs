-- uno0uno/warocol.com#2609 — per-tenant table floor-plan positions.
-- Additive: NULL = unplaced, front falls back to zone-matrix order.

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS pos_x double precision;

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS pos_y double precision;

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS zona varchar(50);

COMMENT ON COLUMN tables.pos_x IS
    'Optional floor-plan X coordinate for tenant tables in POS. NULL = unplaced (legacy order fallback).';
COMMENT ON COLUMN tables.pos_y IS
    'Optional floor-plan Y coordinate for tenant tables in POS. NULL = unplaced (legacy order fallback).';
COMMENT ON COLUMN tables.zona IS
    'Optional floor-plan zone name for tenant tables in POS. NULL = unplaced (legacy order fallback).';

CREATE INDEX IF NOT EXISTS idx_tables_tenant_zona
    ON tables (tenant_id, zona)
    WHERE deleted_at IS NULL;
