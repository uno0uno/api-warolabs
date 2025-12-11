-- ============================================================================
-- Script para poblar inventario histórico de Armelo Perro desde compras
-- Tenant: Armelo Perro (0ffc1252-0bdf-467b-83e0-916213f9f1ec)
-- ============================================================================

-- Verificar estado actual
SELECT '============ ESTADO INICIAL ============' as status;
SELECT
  'Registros en inventario: ' || COUNT(*)::text as info
FROM tenant_inventory
WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT
  'Movimientos históricos: ' || COUNT(*)::text as info
FROM tenant_ingredient_movements
WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT
  'Compras a procesar: ' || COUNT(*)::text || ' compras con ' ||
  SUM((SELECT COUNT(*) FROM tenant_purchase_items WHERE purchase_id = tp.id))::text || ' items' as info
FROM tenant_purchases tp
WHERE tp.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
  AND tp.status IN ('received', 'paid');

-- Procesar compras
DO $$
DECLARE
  v_tenant_id UUID := '0ffc1252-0bdf-467b-83e0-916213f9f1ec';
  v_purchase RECORD;
  v_item RECORD;
  v_current_stock NUMERIC;
  v_new_stock NUMERIC;
  v_purchases_processed INT := 0;
  v_items_processed INT := 0;
  v_items_skipped INT := 0;
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE '============ PROCESANDO COMPRAS ============';
  RAISE NOTICE '';

  -- Iterar compras en orden cronológico
  FOR v_purchase IN (
    SELECT
      id,
      purchase_number,
      purchase_date,
      status,
      received_by
    FROM tenant_purchases
    WHERE tenant_id = v_tenant_id
      AND status IN ('received', 'paid')
    ORDER BY purchase_date ASC
  ) LOOP

    RAISE NOTICE '→ % (%) - %',
      v_purchase.purchase_number,
      v_purchase.status,
      v_purchase.purchase_date::date;

    -- Iterar items de la compra
    FOR v_item IN (
      SELECT
        tpi.id,
        tpi.ingredient_id,
        tpi.quantity,
        tpi.unit,
        tpi.unit_cost,
        i.name as ingredient_name,
        i.controla_inventario
      FROM tenant_purchase_items tpi
      JOIN ingredients i ON tpi.ingredient_id = i.id
      WHERE tpi.purchase_id = v_purchase.id
    ) LOOP

      IF v_item.controla_inventario THEN
        -- Obtener stock actual
        SELECT COALESCE(current_stock, 0)
        INTO v_current_stock
        FROM tenant_inventory
        WHERE tenant_id = v_tenant_id
          AND ingredient_id = v_item.ingredient_id;

        IF v_current_stock IS NULL THEN
          v_current_stock := 0;
        END IF;

        v_new_stock := v_current_stock + v_item.quantity;

        -- Actualizar inventario
        INSERT INTO tenant_inventory (
          tenant_id,
          ingredient_id,
          current_stock,
          minimum_stock,
          last_updated
        )
        VALUES (
          v_tenant_id,
          v_item.ingredient_id,
          v_item.quantity,  -- Primera vez: solo la cantidad
          0,
          v_purchase.purchase_date
        )
        ON CONFLICT (tenant_id, ingredient_id)
        DO UPDATE SET
          current_stock = EXCLUDED.current_stock + tenant_inventory.current_stock,  -- Suma correctamente
          last_updated = EXCLUDED.last_updated;

        -- Crear movimiento
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
        VALUES (
          v_tenant_id,
          v_item.ingredient_id,
          'purchase',
          v_item.quantity,
          v_item.unit,
          'tenant_purchases',
          v_purchase.id,
          v_item.unit_cost,
          'Compra recibida',
          'Compra ' || v_purchase.purchase_number,
          v_purchase.received_by,
          v_current_stock,
          v_new_stock,
          v_purchase.purchase_date
        );

        v_items_processed := v_items_processed + 1;
        RAISE NOTICE '  ✓ %: % → % %',
          v_item.ingredient_name,
          v_current_stock,
          v_new_stock,
          v_item.unit;

      ELSE
        v_items_skipped := v_items_skipped + 1;
      END IF;

    END LOOP;

    v_purchases_processed := v_purchases_processed + 1;

  END LOOP;

  RAISE NOTICE '';
  RAISE NOTICE '============ RESUMEN ============';
  RAISE NOTICE 'Compras procesadas: %', v_purchases_processed;
  RAISE NOTICE 'Items procesados: %', v_items_processed;
  RAISE NOTICE 'Items omitidos: %', v_items_skipped;

END $$;

-- Verificar resultado
SELECT '============ ESTADO FINAL ============' as status;

SELECT
  'Total ingredientes en inventario: ' || COUNT(*)::text as info
FROM tenant_inventory
WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT
  'Total movimientos registrados: ' || COUNT(*)::text as info
FROM tenant_ingredient_movements
WHERE tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec';

SELECT '============ TOP 15 INGREDIENTES ============' as status;

SELECT
  i.name as ingrediente,
  ti.current_stock,
  i.unit
FROM tenant_inventory ti
JOIN ingredients i ON ti.ingredient_id = i.id
WHERE ti.tenant_id = '0ffc1252-0bdf-467b-83e0-916213f9f1ec'
ORDER BY ti.current_stock DESC
LIMIT 15;
