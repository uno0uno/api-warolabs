-- Migration 034: DIAN resolution registry per tenant
-- Issue #116 — DB migrations for electronic invoicing (DIAN/Matias integration)
--
-- Each tenant that wants to issue electronic invoices must have an active resolution
-- granted by the DIAN. The resolution defines:
--   - The authorized prefix (e.g. "FEV") and number range (from_number → to_number)
--   - The validity period (date_from → date_to)
--   - The current consecutive number (current_number), incremented on each emission
--
-- current_number must be incremented with SELECT ... FOR UPDATE in the invoicing
-- microservice to prevent duplicate invoice numbers under concurrent requests.
--
-- Only one resolution per (tenant_id, prefix) can be active at a time — enforced
-- by the partial unique index below.

CREATE TABLE IF NOT EXISTS dian_resolutions (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID        NOT NULL REFERENCES tenants(id),
    resolution_number VARCHAR(50) NOT NULL,   -- DIAN-issued number, e.g. "18760000001"
    prefix            VARCHAR(10) NOT NULL,   -- invoice prefix, e.g. "FEV"
    date_from         DATE        NOT NULL,   -- resolution validity start
    date_to           DATE        NOT NULL,   -- resolution validity end
    from_number       INTEGER     NOT NULL,   -- first authorized invoice number
    to_number         INTEGER     NOT NULL,   -- last authorized invoice number
    current_number    INTEGER     NOT NULL DEFAULT 0,  -- last used number (0 = none yet)
    is_active         BOOLEAN     NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one active resolution per (tenant, prefix) at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_dian_resolutions_active_prefix
    ON dian_resolutions (tenant_id, prefix)
    WHERE is_active = true;

-- Fast lookup of the active resolution for a tenant when emitting an invoice
CREATE INDEX IF NOT EXISTS idx_dian_resolutions_tenant_active
    ON dian_resolutions (tenant_id)
    WHERE is_active = true;
