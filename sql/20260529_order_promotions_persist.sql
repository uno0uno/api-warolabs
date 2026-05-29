-- warocol.com#984 — persist applied promotions on order lines for history/receipts/reports
ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS promo_savings_allocated numeric(10,2);

COMMENT ON COLUMN order_items.promo_savings_allocated IS
    'Promotion savings on this line (COP); separate from manual discount in discount_allocated.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'order_items_applied_promotion_id_fkey'
    ) THEN
        ALTER TABLE order_items
            ADD CONSTRAINT order_items_applied_promotion_id_fkey
            FOREIGN KEY (applied_promotion_id)
            REFERENCES tenant_promotions(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_order_items_applied_promotion_id
    ON order_items (applied_promotion_id)
    WHERE applied_promotion_id IS NOT NULL;
