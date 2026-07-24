-- One-shot backfill: tenant-scoped warehouse articles missing tenant_inventory.
-- Seeds stock 0 / min 0 so they appear on /abastecimiento/stock after #1782 list change.
-- Idempotent via ON CONFLICT DO NOTHING. Safe to re-run.
-- Does NOT touch global ingredients (tenant_id IS NULL).

INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock)
SELECT i.tenant_id, i.id, 0, 0
FROM ingredients i
WHERE i.tenant_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_inventory ti
      WHERE ti.tenant_id = i.tenant_id
        AND ti.ingredient_id = i.id
  )
ON CONFLICT (tenant_id, ingredient_id) DO NOTHING;
