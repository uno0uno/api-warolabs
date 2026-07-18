-- api-warolabs#657: invoice email delivery history + open-tracking signal.
--
-- One row per send attempt of an invoice email. `tracking_token_hash` is the
-- SHA-256 of an opaque token embedded as a 1x1 pixel URL in the HTML body.
-- The raw token is never persisted. No IP, no user-agent.
--
-- `status` semantics:
--   pending — row created before SES call, not yet finalized
--   sent    — SES accepted the request (NOT proof of delivery or human read)
--   failed  — SES rejected the request
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS invoice_email_deliveries (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_id              uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    recipient_email       varchar(320) NOT NULL,
    status                varchar(16) NOT NULL DEFAULT 'pending',
    tracking_token_hash   varchar(64) NOT NULL,
    sent_at               timestamptz,
    failed_at             timestamptz,
    first_opened_at       timestamptz,
    last_opened_at        timestamptz,
    open_count            integer NOT NULL DEFAULT 0,
    failure_code          varchar(120),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE invoice_email_deliveries
    DROP CONSTRAINT IF EXISTS invoice_email_deliveries_status_check;
ALTER TABLE invoice_email_deliveries
    ADD CONSTRAINT invoice_email_deliveries_status_check
    CHECK (status IN ('pending', 'sent', 'failed'));

ALTER TABLE invoice_email_deliveries
    DROP CONSTRAINT IF EXISTS invoice_email_deliveries_open_count_check;
ALTER TABLE invoice_email_deliveries
    ADD CONSTRAINT invoice_email_deliveries_open_count_check
    CHECK (open_count >= 0);

-- Token hash is the lookup key for the public pixel endpoint.
CREATE UNIQUE INDEX IF NOT EXISTS invoice_email_deliveries_token_hash_key
    ON invoice_email_deliveries (tracking_token_hash);

-- Tenant-scoped history per order, newest first.
CREATE INDEX IF NOT EXISTS invoice_email_deliveries_tenant_order_created_idx
    ON invoice_email_deliveries (tenant_id, order_id, created_at DESC);
