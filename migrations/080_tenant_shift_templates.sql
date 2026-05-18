-- 080_tenant_shift_templates.sql
-- Issue warocol.com#680 — Reusable shift template catalog per tenant (epic #678)
--
-- ADD-only: new table only. No changes to accounting_period / closing_summary.
-- #681 adds shift_template_id FK on accounting_period after this lands.

CREATE TABLE IF NOT EXISTS tenant_shift_templates (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name             VARCHAR(80) NOT NULL,
    start_time       TIME NOT NULL,
    end_time         TIME NOT NULL,
    crosses_midnight BOOLEAN NOT NULL DEFAULT false,
    sort_order       SMALLINT NOT NULL DEFAULT 0,
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

COMMENT ON TABLE tenant_shift_templates IS
    'Reusable operating-shift schedules per tenant (warocol.com#680). '
    'Configured under Operaciones → Turnos; resolved to TIMESTAMPTZ windows '
    'per calendar date in #684.';

COMMENT ON COLUMN tenant_shift_templates.crosses_midnight IS
    'When true, end_time is on the day after start_time (e.g. 22:00–06:00).';

CREATE INDEX IF NOT EXISTS idx_shift_templates_tenant
    ON tenant_shift_templates(tenant_id)
    WHERE is_active = true;
