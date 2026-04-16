-- Migration 024: Create journal entries and journal lines tables
-- Double-entry bookkeeping. Every posted entry must have sum(debit) = sum(credit).
-- Balance enforcement is handled at the application layer (service validates before posting).

CREATE TABLE IF NOT EXISTS tenant_journal_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    entry_date DATE NOT NULL,
    period_year INT NOT NULL,
    period_month INT NOT NULL CHECK (period_month BETWEEN 1 AND 12),
    description VARCHAR(500) NOT NULL,
    reference VARCHAR(100),
    source_module VARCHAR(50)
        CHECK (source_module IN ('gastos','ventas','nomina','inventario','arqueo','manual','system')),
    source_id UUID,
    status VARCHAR(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','posted','voided')),
    total_debit NUMERIC(15,2) NOT NULL DEFAULT 0,
    total_credit NUMERIC(15,2) NOT NULL DEFAULT 0,
    created_by UUID,
    posted_at TIMESTAMPTZ,
    voided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_journal_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_entry_id UUID NOT NULL
        REFERENCES tenant_journal_entries(id) ON DELETE CASCADE,
    account_id UUID NOT NULL
        REFERENCES tenant_accounts(id),
    debit NUMERIC(15,2) NOT NULL DEFAULT 0
        CHECK (debit >= 0),
    credit NUMERIC(15,2) NOT NULL DEFAULT 0
        CHECK (credit >= 0),
    description VARCHAR(300),
    line_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_line_debit_or_credit
        CHECK (NOT (debit > 0 AND credit > 0)),
    CONSTRAINT chk_line_not_zero
        CHECK (debit > 0 OR credit > 0)
);

-- Journal entry indexes
CREATE INDEX IF NOT EXISTS idx_je_tenant
    ON tenant_journal_entries(tenant_id);
CREATE INDEX IF NOT EXISTS idx_je_tenant_date
    ON tenant_journal_entries(tenant_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_je_tenant_period
    ON tenant_journal_entries(tenant_id, period_year, period_month);
CREATE INDEX IF NOT EXISTS idx_je_source
    ON tenant_journal_entries(source_module, source_id)
    WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_je_status_posted
    ON tenant_journal_entries(tenant_id, entry_date)
    WHERE status = 'posted';

-- Journal line indexes
CREATE INDEX IF NOT EXISTS idx_jl_entry
    ON tenant_journal_lines(journal_entry_id);
CREATE INDEX IF NOT EXISTS idx_jl_account
    ON tenant_journal_lines(account_id);

COMMENT ON TABLE tenant_journal_entries IS 'Double-entry journal. sum(total_debit) must equal sum(total_credit) for posted entries. Enforced at service layer.';
COMMENT ON COLUMN tenant_journal_entries.period_year IS 'Denormalized from entry_date for fast period GROUP BY queries.';
COMMENT ON COLUMN tenant_journal_entries.period_month IS 'Denormalized from entry_date for fast period GROUP BY queries.';
COMMENT ON COLUMN tenant_journal_entries.total_debit IS 'Denormalized sum of line debits. Kept current by service layer on create/update.';
COMMENT ON COLUMN tenant_journal_entries.source_module IS 'Which WARO module generated this entry. NULL for manual entries.';
COMMENT ON TABLE tenant_journal_lines IS 'Individual debit/credit lines of a journal entry. A line cannot have both debit > 0 and credit > 0.';
