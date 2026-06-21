-- 092_recipe_decimal_scales.sql
-- warocol.com#1429 — preserve recipe quantities with API decimal policy.
--
-- Physical recipe quantities and recipe-base multipliers must keep at least
-- 6 decimals. Purchase/inventory quantity scales are already covered by
-- 091_purchase_inventory_decimal_scales.sql.

ALTER TABLE product_recipes
    ALTER COLUMN quantity TYPE NUMERIC(30, 15) USING ROUND(quantity::numeric, 15);

ALTER TABLE product_base_recipes
    ALTER COLUMN quantity TYPE NUMERIC(30, 15) USING ROUND(quantity::numeric, 15);

COMMENT ON COLUMN product_recipes.quantity IS
    'Ingredient quantity per product recipe with bounded high precision.';

COMMENT ON COLUMN product_base_recipes.quantity IS
    'Recipe-base multiplier per product with bounded high precision.';
