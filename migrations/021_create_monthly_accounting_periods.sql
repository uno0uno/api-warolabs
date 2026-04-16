-- Migration 021: Create monthly accounting periods table for period close feature
-- This enables accountants to close a calendar month, making orders in that month immutable.
-- The daily arqueo (Cierre Z) behavior is unchanged -- only monthly close triggers immutability.

CREATE TABLE IF NOT EXISTS tenant_monthly_periods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    year INT NOT NULL,
    month INT NOT NULL CHECK (month BETWEEN 1 AND 12),
    status VARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_by UUID,
    closed_at TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_year_month UNIQUE (tenant_id, year, month)
);

CREATE INDEX IF NOT EXISTS idx_monthly_periods_tenant_status
    ON tenant_monthly_periods (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_monthly_periods_tenant_year_month
    ON tenant_monthly_periods (tenant_id, year, month);
