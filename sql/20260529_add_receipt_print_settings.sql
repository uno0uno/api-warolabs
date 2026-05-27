-- warocol.com#930 — POS receipt print personalization (epic #929)

ALTER TABLE tenant_fiscal_data
    ADD COLUMN IF NOT EXISTS receipt_document_label varchar(40) NULL DEFAULT 'Prefactura',
    ADD COLUMN IF NOT EXISTS show_logo_on_receipts boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN tenant_fiscal_data.receipt_document_label IS
    'Custom title on POS prefactura/factura tickets (e.g. Prefactura, Orden de compra).';

COMMENT ON COLUMN tenant_fiscal_data.show_logo_on_receipts IS
    'When true, POS thermal prints include tenant logo_url from public profile.';
