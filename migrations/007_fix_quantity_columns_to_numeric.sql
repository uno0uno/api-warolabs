-- Migration: Fix quantity columns from INTEGER to NUMERIC(10,4)
-- Description: Changes all quantity-related columns from INTEGER to NUMERIC to support decimal values
-- Author: Claude Code
-- Date: 2025-12-03
-- Issue: Quantity fields were truncating decimal values (e.g., 125.5 -> 125)

-- =============================================================================
-- PRIORITY HIGH: Core functionality columns
-- =============================================================================

-- 1. base_recipe_templates.base_quantity
-- Used for: Base ingredient quantities in recipe templates
-- Impact: Allows precise measurements like 2.5g, 0.75kg, 125.5g
ALTER TABLE base_recipe_templates
ALTER COLUMN base_quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN base_recipe_templates.base_quantity IS
'Base quantity of ingredient needed for recipe template. NUMERIC(10,4) allows up to 999,999.9999 units with 4 decimal precision.';

-- 2. combo_items.quantity
-- Used for: Quantity of products in combo items
-- Impact: Allows fractional quantities in combos (e.g., 0.5 portions, 1.5 servings)
ALTER TABLE combo_items
ALTER COLUMN quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN combo_items.quantity IS
'Quantity of this product in the combo. NUMERIC(10,4) supports fractional quantities like 0.5 portions.';

-- 3. product_recipe_modifications.quantity_change
-- Used for: Changes to ingredient quantities when modifying recipes
-- Impact: Allows precise quantity adjustments (e.g., +2.5g, -0.75kg)
ALTER TABLE product_recipe_modifications
ALTER COLUMN quantity_change TYPE NUMERIC(10, 4);

COMMENT ON COLUMN product_recipe_modifications.quantity_change IS
'Change in ingredient quantity. Can be positive or negative. NUMERIC(10,4) allows decimal adjustments.';

-- =============================================================================
-- PRIORITY MEDIUM: Order and transaction columns
-- =============================================================================

-- 4. order_items.quantity
-- Used for: Quantity of products in orders
-- Impact: Allows selling fractional quantities (e.g., 0.5kg of product)
-- NOTE: This column is used by v_product_analysis view, so we need to drop and recreate it

-- Save the view definition
CREATE TEMP TABLE temp_view_def AS
SELECT pg_get_viewdef('v_product_analysis'::regclass) AS view_definition;

-- Drop the view
DROP VIEW IF EXISTS v_product_analysis;

-- Now we can alter the column
ALTER TABLE order_items
ALTER COLUMN quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN order_items.quantity IS
'Quantity ordered of this item. NUMERIC(10,4) supports fractional quantities like 0.5kg.';

-- Recreate the view
CREATE OR REPLACE VIEW v_product_analysis AS
WITH product_sales AS (
         SELECT COALESCE(tm.tenant_id, '00000000-0000-0000-0000-000000000000'::uuid) AS tenant_id,
            p.id AS product_id,
            p.name AS product_name,
            c.name AS category_name,
            p.price AS sale_price,
            sum(oi.quantity) AS units_sold,
            sum(oi.subtotal) AS total_revenue,
            count(DISTINCT o.id) AS order_count,
            date_trunc('month'::text, o.order_date)::date AS sales_month
           FROM order_items oi
             JOIN orders o ON oi.order_id = o.id
             JOIN product_variants pv ON oi.variant_id = pv.id
             JOIN product p ON pv.product_id = p.id
             JOIN categories c ON p.category_id = c.id
             LEFT JOIN tenant_members tm ON o.user_id = tm.user_id
          WHERE o.order_date >= (CURRENT_DATE - '3 mons'::interval)
          GROUP BY tm.tenant_id, p.id, p.name, c.name, p.price, (date_trunc('month'::text, o.order_date))
        ), product_metrics AS (
         SELECT ps.tenant_id,
            ps.product_id,
            ps.product_name,
            ps.category_name,
            ps.sale_price,
            ps.units_sold,
            ps.total_revenue,
            ps.order_count,
            ps.sales_month,
            ps.sale_price * 0.40 AS estimated_unit_cost,
            ps.total_revenue * 0.40 AS total_cost,
            ps.total_revenue - ps.total_revenue * 0.40 AS gross_profit,
                CASE
                    WHEN ps.total_revenue > 0::numeric THEN (ps.total_revenue - ps.total_revenue * 0.40) / ps.total_revenue * 100::numeric
                    ELSE 0::numeric
                END AS margin_percentage
           FROM product_sales ps
        ), product_performance AS (
         SELECT pm.tenant_id,
            pm.product_id,
            pm.product_name,
            pm.category_name,
            pm.sale_price,
            pm.units_sold,
            pm.total_revenue,
            pm.order_count,
            pm.sales_month,
            pm.estimated_unit_cost,
            pm.total_cost,
            pm.gross_profit,
            pm.margin_percentage,
                CASE
                    WHEN pm.margin_percentage >= 70::numeric AND pm.units_sold >= 10 THEN round(pm.margin_percentage / 100.0 * (pm.units_sold::numeric / 50.0), 1)
                    WHEN pm.margin_percentage >= 50::numeric AND pm.units_sold >= 5 THEN round(pm.margin_percentage / 100.0 * (pm.units_sold::numeric / 100.0), 1)
                    ELSE round('-0.1'::numeric * (1::numeric - pm.margin_percentage / 100.0), 1)
                END AS tir_impact,
                CASE
                    WHEN pm.margin_percentage >= 70::numeric AND pm.units_sold >= 10 THEN 'Estrella'::text
                    WHEN pm.margin_percentage >= 60::numeric AND pm.units_sold >= 5 THEN 'Potencial'::text
                    WHEN pm.margin_percentage < 50::numeric OR pm.units_sold < 3 THEN 'Problemático'::text
                    ELSE 'Bajo Rendimiento'::text
                END AS classification
           FROM product_metrics pm
        )
 SELECT tenant_id,
    product_id,
    product_name,
    category_name,
    sale_price,
    estimated_unit_cost,
    units_sold,
    total_revenue,
    total_cost,
    gross_profit,
    margin_percentage,
    tir_impact,
    classification,
    sales_month,
    now() AS calculated_at
   FROM product_performance pp
  ORDER BY tenant_id, tir_impact DESC, margin_percentage DESC;

-- 5. order_combo_items.quantity
-- Used for: Quantity of items within combos in orders
-- Impact: Supports fractional quantities in ordered combos
ALTER TABLE order_combo_items
ALTER COLUMN quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN order_combo_items.quantity IS
'Quantity of this item in the ordered combo. NUMERIC(10,4) supports fractional quantities.';

-- 6. order_item_modifiers.quantity
-- Used for: Quantity of modifiers applied to order items
-- Impact: Allows fractional modifier quantities (e.g., 0.5 portions of extra cheese)
ALTER TABLE order_item_modifiers
ALTER COLUMN quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN order_item_modifiers.quantity IS
'Quantity of this modifier applied. NUMERIC(10,4) supports fractional quantities like 0.5 portions.';

-- 7. inventory_transactions.quantity_change
-- Used for: Recording inventory changes (additions/subtractions)
-- Impact: Allows precise inventory tracking with decimals (e.g., +2.5kg, -0.75L)
ALTER TABLE inventory_transactions
ALTER COLUMN quantity_change TYPE NUMERIC(10, 4);

COMMENT ON COLUMN inventory_transactions.quantity_change IS
'Change in inventory quantity. Can be positive (received) or negative (used). NUMERIC(10,4) for precise tracking.';

-- =============================================================================
-- PRIORITY LOW: Secondary functionality columns
-- =============================================================================

-- 8. marketplace_items.stock_quantity
-- Used for: Stock quantity for marketplace items
-- Impact: Allows precise stock tracking with decimal quantities
ALTER TABLE marketplace_items
ALTER COLUMN stock_quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN marketplace_items.stock_quantity IS
'Current stock quantity available. NUMERIC(10,4) supports fractional stock levels.';

-- 9. marketplace_purchases.quantity
-- Used for: Quantity in marketplace purchases
-- Impact: Allows purchasing fractional quantities
ALTER TABLE marketplace_purchases
ALTER COLUMN quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN marketplace_purchases.quantity IS
'Quantity purchased from marketplace. NUMERIC(10,4) supports fractional quantities.';

-- 10. product_variants.stock_quantity
-- Used for: Stock quantity for product variants
-- Impact: Allows precise variant stock tracking
ALTER TABLE product_variants
ALTER COLUMN stock_quantity TYPE NUMERIC(10, 4);

COMMENT ON COLUMN product_variants.stock_quantity IS
'Current stock quantity for this variant. NUMERIC(10,4) supports fractional stock levels.';

-- 11. sale_stages.quantity_available
-- Used for: Available quantity in sale stages
-- Impact: Allows precise tracking of available quantities in sales
ALTER TABLE sale_stages
ALTER COLUMN quantity_available TYPE NUMERIC(10, 4);

COMMENT ON COLUMN sale_stages.quantity_available IS
'Quantity available at this sale stage. NUMERIC(10,4) supports fractional quantities.';

-- =============================================================================
-- VERIFICATION QUERIES
-- =============================================================================

-- Run these queries to verify the changes were applied correctly:

-- Check all modified columns
SELECT
    table_name,
    column_name,
    data_type,
    numeric_precision,
    numeric_scale
FROM information_schema.columns
WHERE table_name IN (
    'base_recipe_templates',
    'combo_items',
    'product_recipe_modifications',
    'order_items',
    'order_combo_items',
    'order_item_modifiers',
    'inventory_transactions',
    'marketplace_items',
    'marketplace_purchases',
    'product_variants',
    'sale_stages'
)
AND column_name IN (
    'base_quantity',
    'quantity',
    'quantity_change',
    'stock_quantity',
    'quantity_available'
)
ORDER BY table_name, column_name;

-- Expected output for all columns:
-- data_type: numeric
-- numeric_precision: 10
-- numeric_scale: 4

-- =============================================================================
-- ROLLBACK (if needed)
-- =============================================================================

-- CAUTION: Rolling back will truncate any decimal values to integers
-- Only use if you need to revert the changes

/*
ALTER TABLE base_recipe_templates ALTER COLUMN base_quantity TYPE INTEGER USING base_quantity::INTEGER;
ALTER TABLE combo_items ALTER COLUMN quantity TYPE INTEGER USING quantity::INTEGER;
ALTER TABLE product_recipe_modifications ALTER COLUMN quantity_change TYPE INTEGER USING quantity_change::INTEGER;
ALTER TABLE order_items ALTER COLUMN quantity TYPE INTEGER USING quantity::INTEGER;
ALTER TABLE order_combo_items ALTER COLUMN quantity TYPE INTEGER USING quantity::INTEGER;
ALTER TABLE order_item_modifiers ALTER COLUMN quantity TYPE INTEGER USING quantity::INTEGER;
ALTER TABLE inventory_transactions ALTER COLUMN quantity_change TYPE INTEGER USING quantity_change::INTEGER;
ALTER TABLE marketplace_items ALTER COLUMN stock_quantity TYPE INTEGER USING stock_quantity::INTEGER;
ALTER TABLE marketplace_purchases ALTER COLUMN quantity TYPE INTEGER USING quantity::INTEGER;
ALTER TABLE product_variants ALTER COLUMN stock_quantity TYPE INTEGER USING stock_quantity::INTEGER;
ALTER TABLE sale_stages ALTER COLUMN quantity_available TYPE INTEGER USING quantity_available::INTEGER;
*/

-- =============================================================================
-- NOTES
-- =============================================================================

/*
NUMERIC(10, 4) specifications:
- Total digits: 10
- Decimal places: 4
- Maximum value: 999,999.9999
- Minimum value: -999,999.9999 (for quantity_change columns)
- Examples: 125.5000, 0.7500, 2.2500, 1000.0000

This precision allows:
- Milligram precision for weight (0.0001g = 0.1mg)
- Milliliter precision for volume (0.0001L = 0.1mL)
- Fractional portions (0.5, 0.25, 0.75, etc.)
- Large quantities up to 1 million units

Columns already using NUMERIC (no changes needed):
- product_recipes.quantity
- modifier_recipes.quantity
- tenant_purchase_items.quantity
- tenant_purchase_items.quantity_received
- tenant_purchase_items.purchase_quantity
- tenant_ingredient_movements.quantity_change
- order_item_ingredients.quantity
*/
