-- Migration 011: Widen remaining NUMERIC(10,4) columns at risk of overflow
-- ingredient_purchase_units.conversion_factor is multiplied during unit resolution —
-- large factors (e.g. tons→mg) can produce results that exceed the old limit.
-- inventory_transactions and product_recipe_modifications handle stock/recipe quantities
-- that can be large for bulk ingredients.

ALTER TABLE ingredient_purchase_units
    ALTER COLUMN conversion_factor TYPE NUMERIC(16, 4);

ALTER TABLE inventory_transactions
    ALTER COLUMN quantity_change TYPE NUMERIC(16, 4);

ALTER TABLE product_recipe_modifications
    ALTER COLUMN quantity_change TYPE NUMERIC(16, 4);
