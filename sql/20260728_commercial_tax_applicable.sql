-- warocol.com#1868 — commercial tax enable/disable flag (ADD only).
ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS commercial_tax_applicable boolean NOT NULL DEFAULT false;

-- Backfill: enabled when any tax_lines entry has rate > 0.
UPDATE tenant_tax_config
SET commercial_tax_applicable = true
WHERE commercial_tax_applicable = false
  AND tax_lines IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(tax_lines) AS elem
      WHERE COALESCE((elem->>'rate')::numeric, 0) > 0
  );
