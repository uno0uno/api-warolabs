-- 081_accounting_period_shift_template_id.sql
-- Issue warocol.com#681 — Optional link from arqueo to shift template (epic #678)
--
-- ADD-only: nullable column + partial index. Depends on 080_tenant_shift_templates.sql (#680).
-- API/UI persistence lands in later issues (#683+).

ALTER TABLE accounting_period
    ADD COLUMN IF NOT EXISTS shift_template_id UUID NULL
    REFERENCES tenant_shift_templates(id) ON DELETE SET NULL;

COMMENT ON COLUMN accounting_period.shift_template_id IS
    'Optional FK to tenant_shift_templates (warocol.com#681). '
    'NULL = custom time window or legacy full-day arqueo.';

CREATE INDEX IF NOT EXISTS idx_accounting_period_shift
    ON accounting_period(shift_template_id)
    WHERE shift_template_id IS NOT NULL;
