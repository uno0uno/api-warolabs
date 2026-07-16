-- api-warolabs#647: included modifier units and auditable excess pricing.
ALTER TABLE modifiers
    ADD COLUMN IF NOT EXISTS included_quantity INTEGER NOT NULL DEFAULT 0;

UPDATE modifiers SET included_quantity = 0 WHERE included_quantity IS NULL;

ALTER TABLE modifiers DROP CONSTRAINT IF EXISTS modifiers_included_quantity_check;
ALTER TABLE modifiers ADD CONSTRAINT modifiers_included_quantity_check
    CHECK (included_quantity >= 0 AND included_quantity <= COALESCE(max_limit, 1));

ALTER TABLE online_cart_item_modifiers
    ADD COLUMN IF NOT EXISTS quantity NUMERIC(10,4) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS included_quantity INTEGER NOT NULL DEFAULT 0;

ALTER TABLE online_cart_item_modifiers
    DROP CONSTRAINT IF EXISTS online_cart_item_modifiers_quantity_positive;
ALTER TABLE online_cart_item_modifiers
    ADD CONSTRAINT online_cart_item_modifiers_quantity_positive CHECK (quantity > 0);

ALTER TABLE pos_cart_item_modifiers
    ADD COLUMN IF NOT EXISTS quantity NUMERIC(10,4) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS included_quantity INTEGER NOT NULL DEFAULT 0;

ALTER TABLE pos_cart_item_modifiers
    DROP CONSTRAINT IF EXISTS pos_cart_item_modifiers_quantity_positive;
ALTER TABLE pos_cart_item_modifiers
    ADD CONSTRAINT pos_cart_item_modifiers_quantity_positive CHECK (quantity > 0);

ALTER TABLE order_item_modifiers
    ADD COLUMN IF NOT EXISTS included_quantity_at_purchase INTEGER NOT NULL DEFAULT 0;

ALTER TABLE online_cart_item_modifiers
    DROP CONSTRAINT IF EXISTS online_cart_item_modifiers_included_quantity_check;
ALTER TABLE online_cart_item_modifiers
    ADD CONSTRAINT online_cart_item_modifiers_included_quantity_check
    CHECK (included_quantity >= 0);

ALTER TABLE pos_cart_item_modifiers
    DROP CONSTRAINT IF EXISTS pos_cart_item_modifiers_included_quantity_check;
ALTER TABLE pos_cart_item_modifiers
    ADD CONSTRAINT pos_cart_item_modifiers_included_quantity_check
    CHECK (included_quantity >= 0);

ALTER TABLE order_item_modifiers
    DROP CONSTRAINT IF EXISTS order_item_modifiers_included_quantity_check;
ALTER TABLE order_item_modifiers
    ADD CONSTRAINT order_item_modifiers_included_quantity_check
    CHECK (included_quantity_at_purchase >= 0);
