-- 079_add_tip_amount_to_orders.sql
-- Issue warocol.com#635 — per-order tip capture.
--
-- Records the tip captured at checkout. The tip is attributed to the
-- existing orders.served_by_member_id (#575) — no new attribution column
-- needed.
--
-- HARD INVARIANT: total_amount NEVER includes tip_amount.
-- _compute_tax_breakdown() in orders_service recomputes tax on every read
-- from order items, so any drift between subtotal and total_amount would
-- inflate the tax base. The charged amount to the customer is computed at
-- the payment layer as total_amount + tip_amount; it is not stored.
--
-- Three CHECK constraints:
--   1. chk_orders_tip_amount_nonneg       — tip cannot be negative
--   2. chk_orders_tip_source              — enum-like guard
--   3. chk_orders_tip_source_consistency  — amount/source must agree
--      (0/none) or (>0 / preset|custom). Prevents subtle data drift.
--
-- Defaults (tip_amount = 0, tip_source = 'none') keep every existing row
-- valid and allow the current checkout flow to keep inserting orders
-- without any code change. App-level wiring lands in warocol.com#637.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS tip_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tip_source VARCHAR(20) NOT NULL DEFAULT 'none';

ALTER TABLE orders
    ADD CONSTRAINT chk_orders_tip_amount_nonneg
        CHECK (tip_amount >= 0);

ALTER TABLE orders
    ADD CONSTRAINT chk_orders_tip_source
        CHECK (tip_source IN ('preset', 'custom', 'none'));

ALTER TABLE orders
    ADD CONSTRAINT chk_orders_tip_source_consistency
        CHECK (
            (tip_amount = 0 AND tip_source = 'none')
            OR (tip_amount > 0 AND tip_source IN ('preset', 'custom'))
        );

COMMENT ON COLUMN orders.tip_amount IS
    'Tip captured at checkout in COP (warocol.com#635). Strictly separate '
    'from total_amount — never folded in. Attributed to served_by_member_id.';

COMMENT ON COLUMN orders.tip_source IS
    'How the tip was selected (warocol.com#635): preset (chip from tenant '
    'config), custom (free input), or none. Used for analytics. Must agree '
    'with tip_amount via chk_orders_tip_source_consistency.';
