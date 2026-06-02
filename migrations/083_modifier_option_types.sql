-- Issue warocol.com#1121: Modifier option types (ingredient | recipe | product | none).
--
-- Existing rows with ingredient_id stay INGREDIENT; price-only rows become NONE.
-- RECIPE uses recipe_base_type_id and/or modifier_recipes; PRODUCT uses linked_product_id.

ALTER TABLE modifiers
    ADD COLUMN IF NOT EXISTS option_type VARCHAR(20) NOT NULL DEFAULT 'INGREDIENT',
    ADD COLUMN IF NOT EXISTS recipe_base_type_id UUID REFERENCES product_base_types(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS recipe_base_quantity NUMERIC(10, 4) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS linked_product_id UUID REFERENCES product(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS linked_product_quantity NUMERIC(10, 4) NOT NULL DEFAULT 1;

ALTER TABLE modifiers DROP CONSTRAINT IF EXISTS check_modifier_option_type;
ALTER TABLE modifiers ADD CONSTRAINT check_modifier_option_type
    CHECK (option_type IN ('INGREDIENT', 'RECIPE', 'PRODUCT', 'NONE'));

UPDATE modifiers
SET option_type = 'NONE'
WHERE ingredient_id IS NULL;

COMMENT ON COLUMN modifiers.option_type IS
    'INGREDIENT: ingredient_id/qty; RECIPE: recipe_base_type_id and/or modifier_recipes; PRODUCT: linked_product composition; NONE: price-only';
