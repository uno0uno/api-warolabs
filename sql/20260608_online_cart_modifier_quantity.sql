ALTER TABLE online_cart_item_modifiers
ADD COLUMN IF NOT EXISTS quantity numeric(10,4) NOT NULL DEFAULT 1;

ALTER TABLE online_cart_item_modifiers
DROP CONSTRAINT IF EXISTS online_cart_item_modifiers_quantity_positive;

ALTER TABLE online_cart_item_modifiers
ADD CONSTRAINT online_cart_item_modifiers_quantity_positive
CHECK (quantity > 0);
