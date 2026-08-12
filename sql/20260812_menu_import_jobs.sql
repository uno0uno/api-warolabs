-- #2254: menu bulk-import jobs (private R2 file + dry-run/commit state)
CREATE TABLE IF NOT EXISTS menu_import_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    uploaded_by UUID NOT NULL REFERENCES profile(id) ON DELETE RESTRICT,
    entity_type VARCHAR(50) NOT NULL DEFAULT 'warehouse',
    status VARCHAR(30) NOT NULL DEFAULT 'uploaded',
    file_name VARCHAR(255) NOT NULL,
    mime_type VARCHAR(100),
    file_size BIGINT,
    s3_key TEXT,
    row_total INT NOT NULL DEFAULT 0,
    row_valid INT NOT NULL DEFAULT 0,
    row_invalid INT NOT NULL DEFAULT 0,
    row_committed INT NOT NULL DEFAULT 0,
    dry_run_report JSONB,
    commit_report JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT menu_import_jobs_status_chk CHECK (
        status IN ('uploaded', 'dry_run', 'committed', 'failed')
    ),
    CONSTRAINT menu_import_jobs_entity_chk CHECK (
        entity_type IN ('warehouse', 'recipe_bases', 'products', 'modifiers')
    )
);

CREATE INDEX IF NOT EXISTS idx_menu_import_jobs_tenant_created
    ON menu_import_jobs (tenant_id, created_at DESC);

COMMENT ON TABLE menu_import_jobs IS
    'Bulk menu/bodega import jobs; original CSV stored in private R2 (s3_key).';
