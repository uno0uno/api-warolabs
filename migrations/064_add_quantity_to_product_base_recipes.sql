-- Issue #517: Per-product multiplier on recipe-base links.
--
-- A product can now declare it consumes Nx of a recipe (e.g.
-- "Hamburguesa Doble" = 2x Salsa BBQ). The multiplier flows through
-- cost calculation, ingredient snapshots, and stock deduction on every
-- sales channel (POS counter/bar, mesa close, online order, admin
-- order modifications).
--
-- Existing rows default to 1, so behavior is unchanged after this
-- migration. Metadata-only on Postgres >=11; no table rewrite.
--
-- The pre-existing UNIQUE (product_id, product_base_type_id, tenant_id)
-- already prevents linking the same recipe twice per product, so no
-- additional constraint is needed.

ALTER TABLE product_base_recipes
    ADD COLUMN IF NOT EXISTS quantity NUMERIC(10, 4) NOT NULL DEFAULT 1;

COMMENT ON COLUMN product_base_recipes.quantity IS
    'How many units of this recipe base the product consumes per order item. Multiplies brt.base_quantity in stock deduction and cost calculation.';
