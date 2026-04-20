-- Migration 038: cesantias_payments table
-- Tracks annual cesantías consignation to employee benefit fund

CREATE TABLE IF NOT EXISTS cesantias_payments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_member_id  UUID NOT NULL,
    anio              INTEGER NOT NULL,
    gross_salary      NUMERIC(12,2) NOT NULL,
    days_worked       INTEGER NOT NULL DEFAULT 360,
    cesantias_amount  NUMERIC(12,2) NOT NULL,
    fondo_name        VARCHAR(100),
    payment_method    VARCHAR(50),
    payment_date      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cesantias_payments_member_anio
    ON cesantias_payments(tenant_member_id, anio);

CREATE INDEX IF NOT EXISTS ix_cesantias_payments_tenant
    ON cesantias_payments(tenant_id);
