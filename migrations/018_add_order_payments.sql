-- Migration 018: add order_payments table for split payment support
-- Safe: ADD only, no DROP, no ALTER on existing tables

CREATE TABLE IF NOT EXISTS order_payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    amount              DECIMAL(12, 2) NOT NULL,
    payment_method      VARCHAR(50),
    payment_method_id   UUID REFERENCES payment_methods(id),
    paid_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id  UUID REFERENCES profile(id),
    notes               TEXT,

    CONSTRAINT chk_order_payment_amount_positive CHECK (amount > 0)
);

CREATE INDEX IF NOT EXISTS idx_order_payments_order_id
    ON order_payments(order_id);

CREATE INDEX IF NOT EXISTS idx_order_payments_tenant_date
    ON order_payments(tenant_id, paid_at);

CREATE INDEX IF NOT EXISTS idx_order_payments_method
    ON order_payments(payment_method)
    WHERE payment_method IS NOT NULL;
