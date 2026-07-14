-- Issue #622: authoritative financial country and base currency per tenant.
-- Additive and idempotent. Existing tenants remain Colombia/COP and no
-- historical amount, order, payment, journal or invoice is rewritten.

CREATE TABLE IF NOT EXISTS tenant_financial_profiles (
    tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    country_code CHAR(2) NOT NULL DEFAULT 'CO',
    base_currency_code CHAR(3) NOT NULL DEFAULT 'COP',
    accounting_localization VARCHAR(50) NOT NULL DEFAULT 'WARO_CO_PUC_V1',
    document_mode VARCHAR(30) NOT NULL DEFAULT 'fiscal_integrated',
    fiscal_provider VARCHAR(30) DEFAULT 'matias',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT tenant_financial_profiles_country_supported CHECK (
        country_code IN (
            'US','CA','GB','AU','NZ','BR','DE','FR','NL','SG','AE','IN',
            'CN','MX','ES','CO','CR','UY','CL','PE','AR','DO','PA'
        )
    ),
    CONSTRAINT tenant_financial_profiles_currency_supported CHECK (
        base_currency_code IN (
            'USD','CAD','GBP','AUD','NZD','BRL','EUR','SGD','AED','INR',
            'CNY','MXN','COP','CRC','UYU','CLP','PEN','ARS','DOP','PAB'
        )
    ),
    CONSTRAINT tenant_financial_profiles_pair_supported CHECK (
        (country_code = 'US' AND base_currency_code = 'USD') OR
        (country_code = 'CA' AND base_currency_code = 'CAD') OR
        (country_code = 'GB' AND base_currency_code = 'GBP') OR
        (country_code = 'AU' AND base_currency_code = 'AUD') OR
        (country_code = 'NZ' AND base_currency_code = 'NZD') OR
        (country_code = 'BR' AND base_currency_code = 'BRL') OR
        (country_code IN ('DE','FR','NL','ES') AND base_currency_code = 'EUR') OR
        (country_code = 'SG' AND base_currency_code = 'SGD') OR
        (country_code = 'AE' AND base_currency_code = 'AED') OR
        (country_code = 'IN' AND base_currency_code = 'INR') OR
        (country_code = 'CN' AND base_currency_code = 'CNY') OR
        (country_code = 'MX' AND base_currency_code = 'MXN') OR
        (country_code = 'CO' AND base_currency_code = 'COP') OR
        (country_code = 'CR' AND base_currency_code = 'CRC') OR
        (country_code = 'UY' AND base_currency_code = 'UYU') OR
        (country_code = 'CL' AND base_currency_code = 'CLP') OR
        (country_code = 'PE' AND base_currency_code = 'PEN') OR
        (country_code = 'AR' AND base_currency_code = 'ARS') OR
        (country_code = 'DO' AND base_currency_code = 'DOP') OR
        (country_code = 'PA' AND base_currency_code IN ('USD','PAB'))
    ),
    CONSTRAINT tenant_financial_profiles_mode_supported CHECK (
        (country_code = 'CO'
            AND accounting_localization = 'WARO_CO_PUC_V1'
            AND document_mode = 'fiscal_integrated'
            AND fiscal_provider = 'matias')
        OR
        (country_code <> 'CO'
            AND accounting_localization = 'WARO_HOSPITALITY_GLOBAL_V1'
            AND document_mode = 'waro_commercial'
            AND fiscal_provider IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_tenant_financial_profiles_country_currency
    ON tenant_financial_profiles(country_code, base_currency_code);

INSERT INTO tenant_financial_profiles (
    tenant_id,
    country_code,
    base_currency_code,
    accounting_localization,
    document_mode,
    fiscal_provider
)
SELECT
    id,
    'CO',
    'COP',
    'WARO_CO_PUC_V1',
    'fiscal_integrated',
    'matias'
FROM tenants
ON CONFLICT (tenant_id) DO NOTHING;

COMMENT ON TABLE tenant_financial_profiles IS
    'Authoritative country/base currency configuration. Changes are guarded by tenant activity locks.';
COMMENT ON COLUMN tenant_financial_profiles.base_currency_code IS
    'Single base currency for all generic monetary amounts; no FX or per-transaction currency.';
