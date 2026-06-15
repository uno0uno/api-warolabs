-- Migration 085: legal document versions and tenant acceptance evidence
--
-- Legal acceptances are electronic evidence and must be retained for at least
-- 10 years. For that reason, acceptance rows keep immutable snapshots and use
-- restrictive foreign keys instead of tenant/document cascades.

CREATE TABLE IF NOT EXISTS legal_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    retention_years INTEGER NOT NULL DEFAULT 10 CHECK (retention_years >= 10),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS legal_document_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES legal_documents(id) ON DELETE RESTRICT,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    effective_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    content_url TEXT,
    content_sha256 TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version)
);

CREATE INDEX IF NOT EXISTS idx_legal_document_versions_current
    ON legal_document_versions (document_id, effective_at DESC, created_at DESC)
    WHERE status = 'published';

CREATE TABLE IF NOT EXISTS legal_document_annexes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_version_id UUID NOT NULL REFERENCES legal_document_versions(id) ON DELETE RESTRICT,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    version TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'global' CHECK (scope_type IN ('global', 'tenant', 'country', 'region')),
    tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT,
    country TEXT,
    region TEXT,
    content_url TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_legal_document_annexes_version
    ON legal_document_annexes (document_version_id, sort_order, code)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_legal_document_annexes_tenant
    ON legal_document_annexes (tenant_id)
    WHERE tenant_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tenant_legal_acceptances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    document_version_id UUID NOT NULL REFERENCES legal_document_versions(id) ON DELETE RESTRICT,
    user_id UUID REFERENCES profile(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'api',
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_ip TEXT,
    user_agent TEXT,
    tenant_name_snapshot TEXT,
    legal_name_snapshot TEXT,
    document_type_snapshot TEXT,
    document_number_snapshot TEXT,
    email_snapshot TEXT,
    actor_name_snapshot TEXT,
    actor_email_snapshot TEXT,
    document_code_snapshot TEXT NOT NULL,
    document_title_snapshot TEXT NOT NULL,
    version_snapshot TEXT NOT NULL,
    annexes_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_version_id)
);

COMMENT ON TABLE tenant_legal_acceptances IS
    'Immutable electronic acceptance evidence for legal document versions. Retain for at least 10 years.';

CREATE INDEX IF NOT EXISTS idx_tenant_legal_acceptances_tenant
    ON tenant_legal_acceptances (tenant_id, accepted_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_legal_acceptances_version
    ON tenant_legal_acceptances (document_version_id);

INSERT INTO legal_documents (code, title, retention_years)
VALUES ('terms_conditions', 'Terminos y Condiciones WARO', 10)
ON CONFLICT (code) DO NOTHING;

INSERT INTO legal_document_versions (
    document_id,
    version,
    status,
    effective_at,
    published_at,
    content_url,
    metadata
)
SELECT
    d.id,
    '1.0',
    'published',
    TIMESTAMPTZ '2026-06-15 00:00:00-05',
    now(),
    '/terminos-y-condiciones',
    jsonb_build_object('source', 'TyC_WARO_v1.0_BORRADOR.pdf')
FROM legal_documents d
WHERE d.code = 'terms_conditions'
ON CONFLICT (document_id, version) DO NOTHING;
