-- warocol.com#1370 — session-scoped advances for minimum consumption / cover.

CREATE TABLE IF NOT EXISTS table_session_advances (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    table_session_id      UUID NOT NULL REFERENCES table_sessions(id),
    amount_cop            NUMERIC(12, 2) NOT NULL,
    payment_method        VARCHAR(50) NOT NULL,
    payment_method_id     UUID REFERENCES payment_methods(id) ON DELETE SET NULL,
    journal_entry_id      UUID REFERENCES tenant_journal_entries(id) ON DELETE SET NULL,
    void_journal_entry_id UUID REFERENCES tenant_journal_entries(id) ON DELETE SET NULL,
    status                VARCHAR(20) NOT NULL DEFAULT 'active',
    notes                 TEXT,
    void_reason           TEXT,
    voided_at             TIMESTAMPTZ,
    voided_by_user_id     UUID REFERENCES profile(id),
    created_by_user_id    UUID REFERENCES profile(id),
    idempotency_key       VARCHAR(128),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_table_session_advances_amount_positive CHECK (amount_cop > 0),
    CONSTRAINT chk_table_session_advances_status CHECK (status IN ('active', 'voided'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_table_session_advances_idempotency
    ON table_session_advances (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_table_session_advances_session_recent
    ON table_session_advances (tenant_id, table_session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_table_session_advances_active_session
    ON table_session_advances (tenant_id, table_session_id)
    WHERE status = 'active';

COMMENT ON TABLE table_session_advances IS
    'Session-scoped minimum consumption / cover advances. Collection is not an order payment or sale.';

COMMENT ON COLUMN table_session_advances.table_session_id IS
    'Open or historical table session that owns the advance; no customer/profile is required.';

ALTER TABLE tenant_journal_entries
    DROP CONSTRAINT IF EXISTS tenant_journal_entries_source_module_check;

ALTER TABLE tenant_journal_entries
    ADD CONSTRAINT tenant_journal_entries_source_module_check
    CHECK (
        source_module IS NULL
        OR source_module::text = ANY (ARRAY[
            'gastos',
            'ventas',
            'orden',
            'orden_cogs',
            'nomina',
            'nomina_provision',
            'nomina_ss',
            'nomina_prima',
            'nomina_cesantias',
            'nomina_int_cesantias',
            'nomina_vacaciones',
            'nomina_dotacion',
            'nomina_pila',
            'nomina_horas_extras',
            'nomina_liquidacion',
            'inventario',
            'cartera',
            'arqueo',
            'manual',
            'system',
            'manual_balance_adjustment',
            'customer_wallet_recharge',
            'customer_wallet_refund',
            'table_session_advance_receive',
            'table_session_advance_void'
        ]::text[])
    );
