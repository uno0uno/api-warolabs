-- ============================================================================
-- Script para poblar inventario histórico desde compras
-- ============================================================================
-- Este script procesa todas las compras recibidas/pagadas de un tenant
-- y crea los registros correspondientes en:
-- 1. tenant_inventory (stock actual)
-- 2. tenant_ingredient_movements (historial de movimientos)
--
-- IMPORTANTE: Solo procesa ingredientes con controla_inventario = true
-- ============================================================================

-- Parámetros
-- Reemplazar con el tenant_id deseado
\set tenant_id '0ffc1252-0bdf-467b-83e0-916213f9f1ec'

-- ============================================================================
-- PASO 1: Verificar estado actual
-- ============================================================================
\echo '============================================================================'
\echo 'ESTADO ACTUAL DEL INVENTARIO'
\echo '============================================================================'

SELECT
  COUNT(*) as registros_inventario,
  COUNT(DISTINCT ingredient_id) as ingredientes_unicos
FROM tenant_inventory
WHERE tenant_id = :'tenant_id';

SELECT
  COUNT(*) as movimientos_historicos
FROM tenant_ingredient_movements
WHERE tenant_id = :'tenant_id';

\echo ''
\echo 'Compras a procesar:'
SELECT
  COUNT(*) as total_compras,
  SUM((SELECT COUNT(*) FROM tenant_purchase_items WHERE purchase_id = tp.id)) as total_items
FROM tenant_purchases tp
WHERE tp.tenant_id = :'tenant_id'
  AND tp.status IN ('received', 'paid');

\echo ''
\echo '============================================================================'
\echo 'INICIANDO POBLADO DE INVENTARIO'
\echo '============================================================================'

-- ============================================================================
-- PASO 2: Procesar cada compra en orden cronológico
-- ============================================================================

DO $$
DECLARE
  v_purchase RECORD;
  v_item RECORD;
  v_ingredient RECORD;
  v_current_stock NUMERIC;
  v_new_stock NUMERIC;
  v_movement_id UUID;
  v_inventory_id UUID;
  v_purchases_processed INT := 0;
  v_items_processed INT := 0;
  v_items_skipped INT := 0;
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Procesando compras en orden cronológico...';
  RAISE NOTICE '';

  -- Iterar sobre cada compra recibida/pagada en orden cronológico
  FOR v_purchase IN (
    SELECT
      id,
      purchase_number,
      purchase_date,
      status,
      received_by
    FROM tenant_purchases
    WHERE tenant_id = :'tenant_id'
      AND status IN ('received', 'paid')
    ORDER BY purchase_date ASC
  ) LOOP

    RAISE NOTICE '→ Procesando compra % (fecha: %)',
      v_purchase.purchase_number,
      v_purchase.purchase_date;

    -- Iterar sobre cada item de la compra
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

      -- Solo procesar si el ingrediente controla inventario
      IF v_item.controla_inventario THEN
        -- Obtener stock actual del ingrediente (o 0 si no existe)
        SELECT COALESCE(current_stock, 0)
        INTO v_current_stock
        FROM tenant_inventory
        WHERE tenant_id = :'tenant_id'
          AND ingredient_id = v_item.ingredient_id;

        -- Si no existe registro, v_current_stock será NULL
        IF v_current_stock IS NULL THEN
          v_current_stock := 0;
        END IF;

        -- Calcular nuevo stock
        v_new_stock := v_current_stock + v_item.quantity;

        -- Insertar o actualizar registro en tenant_inventory
        INSERT INTO tenant_inventory (
          tenant_id,
          ingredient_id,
          current_stock,
          minimum_stock,
          maximum_stock,
          last_updated
        )
        VALUES (
          :'tenant_id',
          v_item.ingredient_id,
          v_new_stock,
          0,  -- minimum_stock por defecto
          NULL,  -- maximum_stock por defecto
          v_purchase.purchase_date
        )
        ON CONFLICT (tenant_id, ingredient_id)
        DO UPDATE SET
          current_stock = tenant_inventory.current_stock + v_item.quantity,
          last_updated = v_purchase.purchase_date
        RETURNING id INTO v_inventory_id;

        -- Crear registro de movimiento
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
          :'tenant_id',
          v_item.ingredient_id,
          'entrada',
          v_item.quantity,  -- Cantidad positiva
          v_item.unit,
          'tenant_purchases',
          v_purchase.id,
          v_item.unit_cost,
          'Compra recibida',
          'Compra ' || v_purchase.purchase_number,
          v_purchase.received_by,  -- Usuario que recibió
          v_current_stock,
          v_new_stock,
          v_purchase.purchase_date  -- Usar fecha de compra para mantener cronología
        )
        RETURNING id INTO v_movement_id;

        v_items_processed := v_items_processed + 1;

        RAISE NOTICE '  ✓ % (%): % → % %',
          v_item.ingredient_name,
          v_item.ingredient_id,
          v_current_stock,
          v_new_stock,
          v_item.unit;

      ELSE
        v_items_skipped := v_items_skipped + 1;
        RAISE NOTICE '  ⊘ % (no controla inventario)',
          v_item.ingredient_name;
      END IF;

    END LOOP;

    v_purchases_processed := v_purchases_processed + 1;
    RAISE NOTICE '';

  END LOOP;

  -- Resumen final
  RAISE NOTICE '';
  RAISE NOTICE '============================================================================';
  RAISE NOTICE 'RESUMEN DEL POBLADO';
  RAISE NOTICE '============================================================================';
  RAISE NOTICE 'Compras procesadas: %', v_purchases_processed;
  RAISE NOTICE 'Items procesados: %', v_items_processed;
  RAISE NOTICE 'Items omitidos (no controla inventario): %', v_items_skipped;
  RAISE NOTICE '';

END $$;

-- ============================================================================
-- PASO 3: Verificar resultado final
-- ============================================================================
\echo '============================================================================'
\echo 'ESTADO FINAL DEL INVENTARIO'
\echo '============================================================================'

SELECT
  COUNT(*) as registros_inventario,
  COUNT(DISTINCT ingredient_id) as ingredientes_unicos,
  SUM(current_stock) as stock_total_items
FROM tenant_inventory
WHERE tenant_id = :'tenant_id';

SELECT
  COUNT(*) as total_movimientos,
  COUNT(DISTINCT ingredient_id) as ingredientes_con_movimientos,
  SUM(quantity_change) as cantidad_total_entradas
FROM tenant_ingredient_movements
WHERE tenant_id = :'tenant_id'
  AND movement_type = 'entrada';

\echo ''
\echo 'Top 10 ingredientes con más stock:'
SELECT
  i.name as ingrediente,
  ti.current_stock,
  i.unit,
  ti.last_updated
FROM tenant_inventory ti
JOIN ingredients i ON ti.ingredient_id = i.id
WHERE ti.tenant_id = :'tenant_id'
ORDER BY ti.current_stock DESC
LIMIT 10;

\echo ''
\echo '============================================================================'
\echo 'POBLADO COMPLETADO'
\echo '============================================================================'
