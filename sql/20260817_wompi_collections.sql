-- uno0uno/api-warolabs#862 — restaurant Wompi collections (BYO merchant).
-- Additive only: ciphertext in Postgres; OpenBao Transit holds the KEK.

CREATE TABLE IF NOT EXISTS tenant_wompi_merchants (
    tenant_id UUID PRIMARY KEY,
    public_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    private_key_ciphertext TEXT NOT NULL,
    events_secret_ciphertext TEXT NOT NULL,
    integrity_secret_ciphertext TEXT,
    payment_method_id UUID REFERENCES payment_methods(id),
    environment TEXT NOT NULL DEFAULT 'test',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE tenant_wompi_merchants IS
    'Per-tenant Wompi diner-collection merchant. Private material is OpenBao Transit ciphertext.';

CREATE TABLE IF NOT EXISTS tenant_wompi_collection_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    order_id UUID NOT NULL,
    amount NUMERIC NOT NULL,
    customer_id UUID,
    link_email TEXT,
    provider_tx_id TEXT,
    provider_link_id TEXT,
    checkout_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    order_payment_id UUID REFERENCES order_payments(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE tenant_wompi_collection_sessions IS
    'Unpaid diner collection until Wompi APPROVED; unique provider_tx_id is the idempotency key.';

CREATE UNIQUE INDEX IF NOT EXISTS tenant_wompi_collection_sessions_provider_tx_uidx
    ON tenant_wompi_collection_sessions (tenant_id, provider_tx_id)
    WHERE provider_tx_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS tenant_wompi_collection_sessions_order_idx
    ON tenant_wompi_collection_sessions (tenant_id, order_id);
