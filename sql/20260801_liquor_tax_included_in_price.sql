-- api-warolabs#764 — CO liquor Incluido/Suma column (ADD only).
ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS liquor_tax_included_in_price boolean NOT NULL DEFAULT false;
