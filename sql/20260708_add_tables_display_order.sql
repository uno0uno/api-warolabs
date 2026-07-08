-- warocol.com#1551 — optional manual POS table order.
-- Additive: existing tenants keep the current API order via the initial backfill.

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS display_order integer;

COMMENT ON COLUMN tables.display_order IS
    'Optional manual display order for regular tenant tables in POS/Operaciones. NULL falls back to legacy name order; bar tables stay pinned by is_bar.';

WITH ordered AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id
            ORDER BY is_active DESC, name
        ) AS rn
    FROM tables
    WHERE deleted_at IS NULL
      AND is_bar IS FALSE
)
UPDATE tables t
SET display_order = ordered.rn
FROM ordered
WHERE t.id = ordered.id
  AND t.display_order IS NULL;

CREATE INDEX IF NOT EXISTS idx_tables_tenant_display_order
    ON tables (tenant_id, is_bar, is_active, display_order, name)
    WHERE deleted_at IS NULL;
