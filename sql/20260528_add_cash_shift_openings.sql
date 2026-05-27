-- warocol.com#920 — shift opening cash float (fondo de caja)
CREATE TABLE IF NOT EXISTS cash_shift_openings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    shift_template_id uuid REFERENCES tenant_shift_templates(id) ON DELETE SET NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    period_start_time timestamptz,
    period_end_time timestamptz,
    opening_cash numeric(14,2) NOT NULL CHECK (opening_cash >= 0),
    opening_breakdown jsonb,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at timestamptz NOT NULL DEFAULT now(),
    opened_by_user_id uuid,
    accounting_period_id uuid REFERENCES accounting_period(id) ON DELETE SET NULL,
    closed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_cash_shift_openings_tenant_status
    ON cash_shift_openings (tenant_id, status, opened_at DESC);

COMMENT ON TABLE cash_shift_openings IS
    'Operational shift open record — fondo de caja before Cierre Z (#920).';

ALTER TABLE closing_summary
    ADD COLUMN IF NOT EXISTS opening_cash numeric(14,2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN closing_summary.opening_cash IS
    'Cash float declared at shift open for this closed period (#920).';
