-- uno0uno/api-warolabs#865 — at most one live unpaid Wompi cobro per order.
-- Additive unique index only.

CREATE UNIQUE INDEX IF NOT EXISTS tenant_wompi_collection_sessions_pending_order_uidx
    ON tenant_wompi_collection_sessions (tenant_id, order_id)
    WHERE status = 'pending';
