-- 083_customer_wallet_ledger.sql
-- Issue api-warolabs#369 — Customer COP prepayment wallet (PUC 2810 liability)
-- Epic warocol.com#1061 batch 1

-- ─────────────────────────────────────────────
-- Tenant config: liability GL code (PUC 2810 default)
-- ─────────────────────────────────────────────

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS customer_wallet_liability_gl_code VARCHAR(20) NOT NULL DEFAULT '2810';

COMMENT ON COLUMN tenant_public_profiles.customer_wallet_liability_gl_code IS
    'PUC account code for customer prepayment liability (api#369). Default 2810.';

-- ─────────────────────────────────────────────
-- Balance row (locked with FOR UPDATE on writes)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS customer_wallet_balances (
    profile_id          UUID NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    balance_cop         NUMERIC(12, 2) NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, tenant_id),
    CONSTRAINT chk_customer_wallet_balance_non_negative CHECK (balance_cop >= 0)
);

COMMENT ON TABLE customer_wallet_balances IS
    'Current COP prepayment balance per customer profile and tenant (api#369).';

-- ─────────────────────────────────────────────
-- Immutable movement ledger
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS customer_wallet_movements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    movement_type       VARCHAR(20) NOT NULL,
    amount_cop          NUMERIC(12, 2) NOT NULL,
    balance_after_cop   NUMERIC(12, 2) NOT NULL,
    payment_method      VARCHAR(50),
    payment_method_id   UUID REFERENCES payment_methods(id),
    order_id            UUID REFERENCES orders(id) ON DELETE SET NULL,
    order_payment_id    UUID REFERENCES order_payments(id) ON DELETE SET NULL,
    journal_entry_id    UUID REFERENCES tenant_journal_entries(id) ON DELETE SET NULL,
    notes               TEXT,
    created_by_user_id  UUID REFERENCES profile(id),
    idempotency_key     VARCHAR(128),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_customer_wallet_movement_type CHECK (
        movement_type IN ('receive', 'apply', 'refund', 'adjust')
    ),
    CONSTRAINT chk_customer_wallet_movement_amount_nonzero CHECK (amount_cop <> 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_wallet_movements_idempotency
    ON customer_wallet_movements (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_customer_wallet_movements_profile_recent
    ON customer_wallet_movements (tenant_id, profile_id, created_at DESC);

COMMENT ON TABLE customer_wallet_movements IS
    'Immutable COP wallet ledger. Positive amount_cop increases balance (receive/adjust+); '
    'negative decreases (apply/refund/adjust-).';

COMMENT ON COLUMN customer_wallet_movements.movement_type IS
    'receive=staff recharge; apply=checkout debit; refund=staff payout; adjust=manual correction.';
