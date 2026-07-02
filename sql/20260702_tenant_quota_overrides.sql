-- api-warolabs#578: tenant-level quota overrides.
-- Non-destructive: these tables only store commercial exceptions and audit
-- history. They do not modify tenant resources or global plan quota metadata.

CREATE TABLE IF NOT EXISTS tenant_quota_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    resource TEXT NOT NULL,
    limit_override INTEGER,
    disabled BOOLEAN NOT NULL DEFAULT false,
    reason TEXT,
    created_by UUID,
    updated_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_quota_overrides_resource_check
        CHECK (resource = ANY (ARRAY[
            'admin_users'::text,
            'active_sessions_per_admin_user'::text,
            'active_kitchens'::text,
            'active_tables_including_bar'::text,
            'active_qr_tables'::text,
            'completed_online_orders_per_month'::text,
            'electronic_invoices_per_period'::text
        ])),
    CONSTRAINT tenant_quota_overrides_limit_check
        CHECK (
            (disabled = true AND limit_override IS NULL)
            OR (disabled = false AND limit_override IS NOT NULL AND limit_override >= 0)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_quota_overrides_tenant_resource
    ON tenant_quota_overrides (tenant_id, resource);

CREATE INDEX IF NOT EXISTS idx_tenant_quota_overrides_tenant
    ON tenant_quota_overrides (tenant_id);

COMMENT ON TABLE tenant_quota_overrides IS
    'Tenant-specific effective quota overrides. disabled=true means unlimited for that resource.';

CREATE TABLE IF NOT EXISTS tenant_quota_override_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    previous_limit_override INTEGER,
    new_limit_override INTEGER,
    previous_disabled BOOLEAN,
    new_disabled BOOLEAN,
    reason TEXT,
    actor_user_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_quota_override_audit_action_check
        CHECK (action = ANY (ARRAY['grant'::text, 'update'::text, 'remove'::text]))
);

CREATE INDEX IF NOT EXISTS idx_tenant_quota_override_audit_tenant_created
    ON tenant_quota_override_audit (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_quota_override_audit_tenant_resource
    ON tenant_quota_override_audit (tenant_id, resource, created_at DESC);

COMMENT ON TABLE tenant_quota_override_audit IS
    'Append-only audit trail for tenant quota override grant/update/remove operations.';
