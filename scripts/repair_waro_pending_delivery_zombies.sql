-- Repair deferred bar deliveries marked completed/paid with zero order_payments.
-- Scope: Waro Colombia tenant only. Skips orders with pending/accepted electronic invoice.
-- Safe to re-run: only touches rows still in the zombie shape.

UPDATE orders
SET status = 'pending',
    payment_status = NULL,
    payment_method = NULL,
    payment_method_id = NULL,
    cash_received = NULL
WHERE id IN (
    SELECT o.id
    FROM orders o
    INNER JOIN tenants tn ON tn.id = o.tenant_id AND tn.name = 'Waro Colombia'
    INNER JOIN table_sessions ts ON ts.id = o.table_session_id
    INNER JOIN tables tb ON tb.id = ts.table_id AND tb.is_bar = TRUE
    WHERE o.delivery_address_id IS NOT NULL
      AND o.status = 'completed'
      AND o.payment_status = 'paid'
      AND NOT EXISTS (
          SELECT 1
          FROM order_payments op
          WHERE op.order_id = o.id AND op.voided_at IS NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM electronic_invoices ei
          WHERE ei.order_id = o.id AND ei.status IN ('pending', 'accepted')
      )
);
