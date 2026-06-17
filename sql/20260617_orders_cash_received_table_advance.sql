ALTER TABLE orders
    DROP CONSTRAINT IF EXISTS chk_orders_cash_received_gte_total;

ALTER TABLE orders
    ADD CONSTRAINT chk_orders_cash_received_gte_total
    CHECK (
        cash_received IS NULL
        OR (
            cash_received >= 0
            AND (
                cash_received >= total_amount
                OR table_session_id IS NOT NULL
            )
        )
    );

COMMENT ON CONSTRAINT chk_orders_cash_received_gte_total ON orders IS
    'Single cash tender must cover total_amount for counter/online orders. Table sessions may store only the cash balance collected after an applied table-session advance; service validation enforces the close amount.';
