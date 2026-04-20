-- Migration 033: Link legal_info to restaurant tenants + add Matias API fiscal fields
-- Issue #116 — DB migrations for electronic invoicing (DIAN/Matias integration)
--
-- legal_info currently stores legal entities for WaRo Tickets promoters via the
-- clusters model (clusters.legal_info_id → legal_info). That relationship stays intact.
--
-- For restaurant tenants (Armelo Perro, Waro Colombia, etc.) that are their own legal
-- entity, we add a direct tenant_id column so the invoicing microservice can look up:
--   SELECT * FROM legal_info WHERE tenant_id = :tenant_id
--
-- tenant_id is NULLABLE — existing rows (WaRo Tickets promoters) cannot be linked
-- directly to a tenant; they are accessed through clusters.legal_info_id.
--
-- Additional columns required by Matias API for the invoice issuer (emisor):
--   phone        — issuer contact phone
--   email        — issuer contact email
--   regime       — tax regime: 'simplified' (simplificado) | 'common' (ordinario)
--   organization — entity type: 'person' (persona natural) | 'company' (persona jurídica)

ALTER TABLE legal_info
    ADD COLUMN IF NOT EXISTS tenant_id    UUID REFERENCES tenants(id),
    ADD COLUMN IF NOT EXISTS phone        VARCHAR(20),
    ADD COLUMN IF NOT EXISTS email        VARCHAR(100),
    ADD COLUMN IF NOT EXISTS regime       VARCHAR(20),
    ADD COLUMN IF NOT EXISTS organization VARCHAR(20);

-- Partial index for fast lookup by the invoicing microservice
-- Only indexes rows that are directly linked to a tenant (restaurant use case)
CREATE INDEX IF NOT EXISTS idx_legal_info_tenant_id
    ON legal_info (tenant_id)
    WHERE tenant_id IS NOT NULL;
