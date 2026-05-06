-- Issue #524: cash tender + change calculation per cash payment line.
--
-- The cashier records how much cash the customer handed over and the
-- system derives the change (vuelto) live. Persisted on:
--   * order_payments.cash_received — for split-tender cash lines.
--   * orders.cash_received — for single-payment (non-split) cash sales,
--     because the existing flow does NOT insert order_payments rows for
--     single payments (cierre relies on that absence as a "legacy" fallback
--     to avoid double-counting).
--
-- Both columns are nullable; non-cash methods leave NULL. Existing rows
-- stay NULL — backwards compatible. Metadata-only on PG >=11 (no table
-- rewrite).

-- Per-line cash tender (split mode)
ALTER TABLE order_payments
    ADD COLUMN IF NOT EXISTS cash_received NUMERIC(12, 2) NULL;

ALTER TABLE order_payments
    ADD CONSTRAINT chk_order_payment_cash_received_gte_amount
    CHECK (cash_received IS NULL OR cash_received >= amount);

COMMENT ON COLUMN order_payments.cash_received IS
    'Cash handed over by the customer for this split payment line. Always NULL for non-cash methods. Change due is derived: cash_received - amount.';

-- Order-level cash tender (single-payment cash sales — flow does not insert order_payments rows)
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS cash_received NUMERIC(12, 2) NULL;

ALTER TABLE orders
    ADD CONSTRAINT chk_orders_cash_received_gte_total
    CHECK (cash_received IS NULL OR cash_received >= total_amount);

COMMENT ON COLUMN orders.cash_received IS
    'Cash handed over by the customer for the entire order, used only when the order has a single cash payment (no order_payments rows). Always NULL for split-mode orders or non-cash sales. Change due is derived: cash_received - total_amount.';
