-- warocol.com#785 — persist payment void reason on order_payments
ALTER TABLE order_payments
    ADD COLUMN IF NOT EXISTS void_reason TEXT NULL;

COMMENT ON COLUMN order_payments.void_reason IS
    'Motivo de anulación (#649/#785). NULL mientras el pago está activo.';
