-- Migration 035: Electronic invoices registry
-- Issue #116 — DB migrations for electronic invoicing (DIAN/Matias integration)
--
-- Stores the result of each invoice emitted through the Matias API / DIAN.
-- Required by DIAN Resolución 000165 (Nov 2023): both PDF and XML must be
-- retained for a minimum of 5 years from January 1 of the following year.
--
-- r2_pdf_key / r2_xml_key: object paths in Cloudflare R2 private bucket
--   (warocol-purchase-attachments), NOT full URLs. Presigned URLs are
--   generated on demand via GET /orders/{id}/invoice (issue #118).
--
-- order_id is intentionally not a FK: POS orders and online delivery orders
-- both land in the same `orders` table but come from different flows.
-- The invoicing microservice resolves the order regardless of origin.
--
-- status lifecycle: pending → sent → accepted | rejected

CREATE TABLE IF NOT EXISTS electronic_invoices (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id),
    order_id        UUID        NOT NULL,            -- no FK: shared across POS + delivery
    order_type      VARCHAR(20) NOT NULL,            -- 'pos' | 'delivery'
    invoice_number  INTEGER     NOT NULL,
    prefix          VARCHAR(10) NOT NULL,
    cufe            VARCHAR(100),                    -- SHA-256 hash returned by DIAN/Matias
    matias_uuid     VARCHAR(100),                    -- Matias internal document UUID
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|sent|accepted|rejected
    r2_pdf_key      TEXT,                            -- R2 path: invoices/{tenant_id}/{year}/{month}/{prefix}-{number}/{cufe}.pdf
    r2_xml_key      TEXT,                            -- R2 path: invoices/{tenant_id}/{year}/{month}/{prefix}-{number}/{cufe}.xml
    matias_response JSONB,                           -- full Matias API response for audit
    error_message   TEXT,                            -- populated on rejection or API error
    emitted_at      TIMESTAMPTZ,                     -- timestamp of successful DIAN acceptance
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- No two invoices with the same number+prefix per tenant
    CONSTRAINT uq_electronic_invoices_number UNIQUE (tenant_id, prefix, invoice_number)
);

-- Fast lookup from GET /orders/{id}/invoice (issue #118)
CREATE INDEX IF NOT EXISTS idx_electronic_invoices_order_id
    ON electronic_invoices (order_id);

-- Reporting: list all invoices for a tenant ordered by emission date
CREATE INDEX IF NOT EXISTS idx_electronic_invoices_tenant_emitted
    ON electronic_invoices (tenant_id, emitted_at DESC NULLS LAST);
