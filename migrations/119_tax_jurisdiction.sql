-- Issue warocol.com#1848: US state / CA province tax jurisdiction.
-- ADD-only: nullable jurisdiction code next to tax_lines.

ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS tax_jurisdiction_code VARCHAR(10);

COMMENT ON COLUMN tenant_tax_config.tax_jurisdiction_code IS
    'US state or CA province/territory code (e.g. TX, ON). '
    'Null until user selects jurisdiction; drives static tax_lines seed.';
