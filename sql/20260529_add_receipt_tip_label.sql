-- warocol.com#977 — customizable tip line label on POS tickets

ALTER TABLE tenant_fiscal_data
    ADD COLUMN IF NOT EXISTS receipt_tip_label varchar(40) NULL DEFAULT 'Propina';

COMMENT ON COLUMN tenant_fiscal_data.receipt_tip_label IS
    'Custom label for the tip line on POS prefactura/receipt/email (e.g. Propina, Servicio).';
