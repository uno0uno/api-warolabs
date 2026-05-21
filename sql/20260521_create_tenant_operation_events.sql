-- Bitácora de operaciones — append-only POS audit events (warocol.com#782)
CREATE TABLE IF NOT EXISTS tenant_operation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    domain TEXT NOT NULL,
    channel TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_user_id UUID,
    actor_member_id UUID,
    table_id UUID,
    table_session_id UUID,
    pos_cart_id UUID,
    order_id UUID,
    order_item_id UUID,
    comanda_item_id UUID,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT,
    CONSTRAINT tenant_operation_events_domain_check
        CHECK (domain = ANY (ARRAY['pos'::text])),
    CONSTRAINT tenant_operation_events_channel_check
        CHECK (channel = ANY (ARRAY['mesa'::text, 'barra'::text, 'mostrador'::text])),
    CONSTRAINT tenant_operation_events_action_check
        CHECK (action = ANY (ARRAY[
            'tab_item_added'::text,
            'tab_item_removed'::text,
            'tab_item_qty_changed'::text,
            'tab_cleared'::text,
            'cart_line_removed'::text,
            'cart_cleared'::text,
            'payment_voided'::text,
            'comanda_line_cancelled'::text
        ]))
);

COMMENT ON TABLE tenant_operation_events IS
    'Append-only operation audit log for Bitácora de operaciones (MVP: POS).';

CREATE INDEX IF NOT EXISTS idx_tenant_operation_events_tenant_created
    ON tenant_operation_events (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_operation_events_tenant_action
    ON tenant_operation_events (tenant_id, action);

CREATE INDEX IF NOT EXISTS idx_tenant_operation_events_tenant_actor_user
    ON tenant_operation_events (tenant_id, actor_user_id)
    WHERE actor_user_id IS NOT NULL;
