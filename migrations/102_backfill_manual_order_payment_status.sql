-- Issue #619: manual orders historically omitted orders.payment_status.
-- Only repair rows whose persisted payment evidence makes the target status unambiguous.

-- Pure credit: no credit collection and no active split tender means the full
-- completed order remains receivable. Require a real, non-anonymous customer so
-- the repaired row can safely participate in cartera.
UPDATE orders AS o
SET payment_status = 'credit'
WHERE o.extra_attributes->>'source' = 'manual'
  AND o.status = 'completed'
  AND o.payment_status IS NULL
  AND o.payment_method = 'credit'
  AND o.credit_paid_amount = 0
  AND EXISTS (
      SELECT 1
      FROM profile AS p
      WHERE p.id = o.customer_id
        AND p.phone_number IS DISTINCT FROM '0000000000'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM order_payments AS op
      WHERE op.order_id = o.id
        AND op.voided_at IS NULL
  )
  AND NOT EXISTS (
      SELECT 1
      FROM credit_payments AS cp
      WHERE cp.order_id = o.id
  );

-- Paid manual orders are either single non-credit tenders (which intentionally
-- have no order_payments rows) or split tenders whose active rows cover the
-- complete order total. Voided/incomplete splits remain untouched for review.
UPDATE orders AS o
SET payment_status = 'paid'
WHERE o.extra_attributes->>'source' = 'manual'
  AND o.status = 'completed'
  AND o.payment_status IS NULL
  AND (
      (
          o.payment_method IS NOT NULL
          AND o.payment_method <> 'credit'
          AND NOT EXISTS (
              SELECT 1
              FROM order_payments AS op
              WHERE op.order_id = o.id
          )
      )
      OR
      (
          EXISTS (
              SELECT 1
              FROM order_payments AS op
              WHERE op.order_id = o.id
                AND op.voided_at IS NULL
          )
          AND ABS(
              COALESCE(
                  (
                      SELECT SUM(op.amount)
                      FROM order_payments AS op
                      WHERE op.order_id = o.id
                        AND op.voided_at IS NULL
                  ),
                  0
              ) - o.total_amount
          ) <= 0.01
      )
  );
