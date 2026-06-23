ALTER TABLE tenant_fiscal_data
  ADD COLUMN IF NOT EXISTS matias_company_id text;

COMMENT ON COLUMN tenant_fiscal_data.matias_company_id IS
  'Matias Casa de Software customer client_uuid for this tenant; not the WARO tenant_id.';
