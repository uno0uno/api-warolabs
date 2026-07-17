-- Migration 114: explicit DIAN/Matias sales-tax profile per tenant.
--
-- The previous model stored issuer IVA responsibility separately from the
-- INC/IVA calculation toggles. That allowed contradictory combinations such
-- as "No responsable de IVA" with IVA enabled, or a no-tax restaurant without
-- explicitly confirming its INC responsibility.

ALTER TABLE tenant_fiscal_data
    ADD COLUMN sales_tax_profile varchar(40) NOT NULL DEFAULT 'unconfigured';

ALTER TABLE tenant_fiscal_data
    ADD CONSTRAINT tenant_fiscal_data_sales_tax_profile_check
    CHECK (
        sales_tax_profile IN (
            'unconfigured',
            'iva_responsible',
            'inc_responsible',
            'non_responsible_iva_inc',
            'non_responsible_iva'
        )
    );

-- Preserve unambiguous existing configurations. No-tax rows remain
-- unconfigured because the previous booleans cannot distinguish RUT code 50
-- (No responsable de INC) from "INC no aplica".
UPDATE tenant_fiscal_data AS fd
SET sales_tax_profile = CASE
    WHEN COALESCE(ttc.iva_applicable, false)
         AND NOT COALESCE(ttc.inc_applicable, false)
        THEN 'iva_responsible'
    WHEN COALESCE(ttc.inc_applicable, false)
         AND NOT COALESCE(ttc.iva_applicable, false)
        THEN 'inc_responsible'
    ELSE 'unconfigured'
END
FROM tenant_tax_config AS ttc
WHERE ttc.tenant_id = fd.tenant_id;

COMMENT ON COLUMN tenant_fiscal_data.sales_tax_profile IS
    'Authoritative WARO sales-tax profile aligned with DIAN/Matias: '
    'iva_responsible, inc_responsible, non_responsible_iva_inc, '
    'non_responsible_iva, or unconfigured.';
