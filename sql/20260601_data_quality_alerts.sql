-- api-warolabs#46 — purchase data quality alerts (price spike / unit mismatch)
CREATE TABLE IF NOT EXISTS data_quality_alerts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    purchase_item_id uuid REFERENCES tenant_purchase_items(id) ON DELETE SET NULL,
    ingredient_id uuid REFERENCES ingredients(id) ON DELETE SET NULL,
    ingredient_name varchar(255) NOT NULL,
    alert_type varchar(50) NOT NULL,
    severity varchar(20) NOT NULL,
    expected_value numeric,
    actual_value numeric,
    deviation_pct numeric,
    rolling_avg numeric,
    context jsonb,
    resolved boolean NOT NULL DEFAULT false,
    resolved_by uuid REFERENCES profile(id) ON DELETE SET NULL,
    resolved_at timestamptz,
    resolution_note text,
    original_value numeric,
    corrected_value numeric,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_dqa_item_tenant UNIQUE (purchase_item_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_dqa_tenant_resolved
    ON data_quality_alerts (tenant_id, resolved);

CREATE INDEX IF NOT EXISTS idx_dqa_severity
    ON data_quality_alerts (severity, resolved);

CREATE INDEX IF NOT EXISTS idx_dqa_purchase_item
    ON data_quality_alerts (purchase_item_id)
    WHERE purchase_item_id IS NOT NULL;

COMMENT ON TABLE data_quality_alerts IS
    'Anomaly alerts on purchase line items — IQR + % deviation (#46).';
