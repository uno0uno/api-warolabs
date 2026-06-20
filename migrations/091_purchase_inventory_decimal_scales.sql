-- 091_purchase_inventory_decimal_scales.sql
-- warocol.com#1419 — bound purchase/inventory decimal scales.
--
-- Quantity, stock, conversion factor, and base-unit cost columns keep high
-- internal precision for domain math. Money totals are rounded to currency
-- precision so historical binary-float tails stop propagating.

ALTER TABLE tenant_purchase_items
    ALTER COLUMN quantity TYPE NUMERIC(30, 15) USING ROUND(quantity::numeric, 15),
    ALTER COLUMN purchase_quantity TYPE NUMERIC(30, 15) USING ROUND(purchase_quantity::numeric, 15),
    ALTER COLUMN quantity_received TYPE NUMERIC(30, 15) USING ROUND(quantity_received::numeric, 15),
    ALTER COLUMN unit_cost TYPE NUMERIC(30, 15) USING ROUND(unit_cost::numeric, 15),
    ALTER COLUMN total_cost TYPE NUMERIC(18, 2) USING ROUND(total_cost::numeric, 2);

ALTER TABLE tenant_inventory
    ALTER COLUMN current_stock TYPE NUMERIC(30, 15) USING ROUND(current_stock::numeric, 15),
    ALTER COLUMN minimum_stock TYPE NUMERIC(30, 15) USING ROUND(minimum_stock::numeric, 15),
    ALTER COLUMN maximum_stock TYPE NUMERIC(30, 15) USING ROUND(maximum_stock::numeric, 15);

ALTER TABLE tenant_ingredient_movements
    ALTER COLUMN quantity_change TYPE NUMERIC(30, 15) USING ROUND(quantity_change::numeric, 15),
    ALTER COLUMN previous_stock TYPE NUMERIC(30, 15) USING ROUND(previous_stock::numeric, 15),
    ALTER COLUMN new_stock TYPE NUMERIC(30, 15) USING ROUND(new_stock::numeric, 15),
    ALTER COLUMN cost_per_unit TYPE NUMERIC(30, 15) USING ROUND(cost_per_unit::numeric, 15);

ALTER TABLE ingredient_purchase_units
    ALTER COLUMN conversion_factor TYPE NUMERIC(30, 15) USING ROUND(conversion_factor::numeric, 15),
    ALTER COLUMN unit_cost TYPE NUMERIC(18, 2) USING ROUND(unit_cost::numeric, 2);

COMMENT ON COLUMN tenant_purchase_items.quantity IS
    'Base quantity with bounded high precision for purchase/inventory math.';
COMMENT ON COLUMN tenant_purchase_items.purchase_quantity IS
    'Original purchase-unit quantity with bounded high precision.';
COMMENT ON COLUMN tenant_purchase_items.quantity_received IS
    'Received base quantity with bounded high precision.';
COMMENT ON COLUMN tenant_purchase_items.unit_cost IS
    'Base-unit cost with bounded high precision; display layers format as money when needed.';
COMMENT ON COLUMN tenant_purchase_items.total_cost IS
    'Purchase item total rounded to currency precision.';

COMMENT ON COLUMN tenant_inventory.current_stock IS
    'Current stock with bounded high precision.';
COMMENT ON COLUMN tenant_inventory.minimum_stock IS
    'Minimum stock threshold with bounded high precision.';
COMMENT ON COLUMN tenant_inventory.maximum_stock IS
    'Maximum stock threshold with bounded high precision.';

COMMENT ON COLUMN tenant_ingredient_movements.quantity_change IS
    'Movement quantity with bounded high precision.';
COMMENT ON COLUMN tenant_ingredient_movements.previous_stock IS
    'Previous stock snapshot with bounded high precision.';
COMMENT ON COLUMN tenant_ingredient_movements.new_stock IS
    'New stock snapshot with bounded high precision.';
COMMENT ON COLUMN tenant_ingredient_movements.cost_per_unit IS
    'Movement base-unit cost with bounded high precision.';

COMMENT ON COLUMN ingredient_purchase_units.conversion_factor IS
    'Purchase-unit to base-unit factor with bounded high precision.';
COMMENT ON COLUMN ingredient_purchase_units.unit_cost IS
    'Purchase-unit money cost rounded to currency precision.';
