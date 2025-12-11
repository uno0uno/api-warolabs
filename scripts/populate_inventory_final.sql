-- ============================================================================
-- Script FINAL para poblar inventario desde compras
-- Usa el trigger automático para actualizar tenant_inventory
-- ============================================================================

SELECT '============ LIMPIANDO DATOS ANTERIORES ============' as status;
DELETE FROM tenant_ingredient_movements WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';
DELETE FROM tenant_inventory WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT '============ INSERTANDO MOVIMIENTOS (el trigger actualizará inventory) ============' as status;

-- Solo insertar movimientos - el trigger update_inventory_on_movement() hará el resto
WITH purchases_ordered AS (
  SELECT
    tp.id as purchase_id,
    tp.purchase_number,
    tp.purchase_date,
    tp.received_by,
    tpi.ingredient_id,
    tpi.quantity,
    tpi.unit,
    tpi.unit_cost,
    i.name as ingredient_name,
    ROW_NUMBER() OVER (ORDER BY tp.purchase_date ASC, tpi.id ASC) as row_num
  FROM tenant_purchases tp
  JOIN tenant_purchase_items tpi ON tp.id = tpi.purchase_id
  JOIN ingredients i ON tpi.ingredient_id = i.id
  WHERE tp.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
    AND tp.status IN ('received', 'paid')
    AND i.controla_inventario = true
  ORDER BY tp.purchase_date ASC, tpi.id ASC
),
cumulative_stock AS (
  SELECT
    purchase_id,
    purchase_number,
    purchase_date,
    received_by,
    ingredient_id,
    quantity,
    unit,
    unit_cost,
    ingredient_name,
    row_num,
    COALESCE(SUM(quantity) OVER (
      PARTITION BY ingredient_id
      ORDER BY row_num
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ), 0) as previous_stock,
    SUM(quantity) OVER (
      PARTITION BY ingredient_id
      ORDER BY row_num
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) as new_stock
  FROM purchases_ordered
)
INSERT INTO tenant_ingredient_movements (
  tenant_id,
  ingredient_id,
  movement_type,
  quantity_change,
  unit,
  reference_table,
  reference_id,
  cost_per_unit,
  reason,
  notes,
  created_by,
  previous_stock,
  new_stock,
  created_at
)
SELECT
  '0ffc1252-0bdf-467b-83e0-916213f9f1ec'::uuid,
  ingredient_id,
  'purchase',
  quantity,
  unit,
  'tenant_purchases',
  purchase_id,
  unit_cost,
  'Compra recibida',
  'Compra ' || purchase_number,
  received_by,
  previous_stock,
  new_stock,
  purchase_date
FROM cumulative_stock
ORDER BY row_num;

SELECT '============ ESTADO FINAL ============' as status;

SELECT
  'Total ingredientes en inventario: ' || COUNT(*)::text as info
FROM tenant_inventory
WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT
  'Total movimientos registrados: ' || COUNT(*)::text as info
FROM tenant_ingredient_movements
WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT '============ VERIFICACIÓN CRÍTICA ============' as status;

SELECT
  i.name,
  ti.current_stock as stock_actual,
  (SELECT SUM(tpi.quantity)
   FROM tenant_purchases tp
   JOIN tenant_purchase_items tpi ON tp.id = tpi.purchase_id
   WHERE tp.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
     AND tp.status IN ('received', 'paid')
     AND tpi.ingredient_id = i.id) as total_comprado,
  CASE
    WHEN ti.current_stock = (SELECT SUM(tpi.quantity)
       FROM tenant_purchases tp
       JOIN tenant_purchase_items tpi ON tp.id = tpi.purchase_id
       WHERE tp.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
         AND tp.status IN ('received', 'paid')
         AND tpi.ingredient_id = i.id)
    THEN '✓ CORRECTO'
    ELSE '✗ ERROR'
  END as verificacion
FROM tenant_inventory ti
JOIN ingredients i ON ti.ingredient_id = i.id
WHERE ti.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
  AND i.name IN ('Carne de Res', 'Papa Ripio', 'Papas Fritas', 'Tomate', 'Aceite')
ORDER BY i.name;

SELECT '============ TOP 10 INGREDIENTES ============' as status;

SELECT
  i.name as ingrediente,
  ti.current_stock,
  i.unit
FROM tenant_inventory ti
JOIN ingredients i ON ti.ingredient_id = i.id
WHERE ti.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
ORDER BY ti.current_stock DESC
LIMIT 10;

SELECT '✅ POBLADO COMPLETADO EXITOSAMENTE' as status;
