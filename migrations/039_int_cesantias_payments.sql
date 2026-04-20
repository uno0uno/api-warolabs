-- Migration 039: int_cesantias_payments table
-- Tracks annual intereses sobre cesantías payments (paid directly to employee)

CREATE TABLE IF NOT EXISTS int_cesantias_payments (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_member_id      UUID NOT NULL,
    anio                  INTEGER NOT NULL,
    cesantias_base        NUMERIC(12,2) NOT NULL,
    int_cesantias_amount  NUMERIC(12,2) NOT NULL,
    payment_method        VARCHAR(50),
    payment_date          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_int_cesantias_payments_member_anio
    ON int_cesantias_payments(tenant_member_id, anio);

CREATE INDEX IF NOT EXISTS ix_int_cesantias_payments_tenant
    ON int_cesantias_payments(tenant_id);
