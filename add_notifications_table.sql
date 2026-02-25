-- =============================================================================
-- Notifications Table
-- Issue: feat(notifications): add notifications table, service, and REST endpoints
-- =============================================================================

CREATE TABLE IF NOT EXISTS notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_id    UUID REFERENCES orders(id) ON DELETE SET NULL,
    type        VARCHAR(50) NOT NULL DEFAULT 'new_order',
    payload     JSONB NOT NULL DEFAULT '{}',
    read_at     TIMESTAMPTZ NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial index for fast unread queries per tenant
CREATE INDEX IF NOT EXISTS idx_notifications_tenant_unread
    ON notifications (tenant_id, created_at DESC)
    WHERE read_at IS NULL;

-- Index for lookups by order
CREATE INDEX IF NOT EXISTS idx_notifications_order_id
    ON notifications (order_id);
