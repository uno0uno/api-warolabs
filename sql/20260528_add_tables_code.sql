-- warocol.com#927 — optional short code for POS floor plan tiles

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS code varchar(4) NULL;

COMMENT ON COLUMN tables.code IS
    'Short POS display code (max 4 chars). Optional; inferred from name when blank.';

CREATE UNIQUE INDEX IF NOT EXISTS tables_tenant_code_unique
    ON tables (tenant_id, upper(code))
    WHERE code IS NOT NULL AND deleted_at IS NULL;
