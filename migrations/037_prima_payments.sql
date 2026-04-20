-- Migration 037: prima_payments table
-- Tracks actual prima de servicios disbursements (distinct from monthly provisions)
-- Issue #405
--
-- Safety: ADD-only — no DROP, no ALTER of existing rows.
-- Prerequisite: migration 036 (salary_provisions) must be applied to prod before deploying.

CREATE TABLE IF NOT EXISTS prima_payments (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_member_id UUID NOT NULL,
    semestre         VARCHAR(7) NOT NULL,  -- format: '2025-S1' (Jan–Jun) or '2025-S2' (Jul–Dec)
    gross_salary     NUMERIC(12,2) NOT NULL,
    days_worked      INTEGER NOT NULL DEFAULT 180,
    prima_amount     NUMERIC(12,2) NOT NULL,
    payment_method   VARCHAR(50),
    payment_date     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_prima_payments_member_semestre
    ON prima_payments(tenant_member_id, semestre);

CREATE INDEX IF NOT EXISTS ix_prima_payments_tenant
    ON prima_payments(tenant_id);
