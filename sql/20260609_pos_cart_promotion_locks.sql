ALTER TABLE pos_cart_items
    ADD COLUMN IF NOT EXISTS locked_promotion_id uuid,
    ADD COLUMN IF NOT EXISTS promotion_locked_at timestamptz,
    ADD COLUMN IF NOT EXISTS locked_promo_eligible_subtotal numeric(10,2),
    ADD COLUMN IF NOT EXISTS locked_promo_eligible_unit_price numeric(10,2),
    ADD COLUMN IF NOT EXISTS locked_promotion_name text,
    ADD COLUMN IF NOT EXISTS locked_promo_type text,
    ADD COLUMN IF NOT EXISTS locked_promo_savings numeric(10,2);

COMMENT ON COLUMN pos_cart_items.locked_promotion_id IS
    'Promotion captured when the POS cart line was last created or materially edited while eligible.';
COMMENT ON COLUMN pos_cart_items.promotion_locked_at IS
    'Timestamp used to evaluate and lock the line promotion.';
COMMENT ON COLUMN pos_cart_items.locked_promo_eligible_subtotal IS
    'Line subtotal basis eligible for the locked promotion.';
COMMENT ON COLUMN pos_cart_items.locked_promo_eligible_unit_price IS
    'Per-unit basis eligible for the locked promotion, including required/default modifiers.';
COMMENT ON COLUMN pos_cart_items.locked_promo_savings IS
    'Authoritative promotion savings captured for the locked POS cart line.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'pos_cart_items_locked_promotion_id_fkey'
    ) THEN
        ALTER TABLE pos_cart_items
            ADD CONSTRAINT pos_cart_items_locked_promotion_id_fkey
            FOREIGN KEY (locked_promotion_id)
            REFERENCES tenant_promotions(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_pos_cart_items_locked_promotion_id
    ON pos_cart_items (locked_promotion_id)
    WHERE locked_promotion_id IS NOT NULL;
