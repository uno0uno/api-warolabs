-- Migration 027: Per-tenant tax configuration
-- Issue #378 — Required before auto-posting ventas/arqueo to GL
--
-- Colombian tax regimes are mutually exclusive per establishment type:
--   inc         → Impuesto Nacional al Consumo (Ley 1607/2012, art. 71-90)
--                 Restaurants/bars in ordinary regime: 8%
--   iva         → IVA (régimen común) — establecimientos que emiten factura IVA
--   liquor      → Impoconsumo de licores (different rate, managed by dept.)
--   simplified  → Régimen simplificado — small taxpayers, no tax posting needed
--
-- A tenant may have inc_applicable=true AND iva_applicable=true if they sell
-- both food (INC) and other goods (IVA) but this is unusual.
-- liquor_tax_applicable is independent and can stack with inc or iva.

CREATE TABLE IF NOT EXISTS tenant_tax_config (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- INC — Impuesto Nacional al Consumo
    inc_applicable          BOOLEAN NOT NULL DEFAULT false,
    inc_rate                NUMERIC(6,4) NOT NULL DEFAULT 0.0800,  -- 8.00%
    inc_gl_account_code     VARCHAR(20) NOT NULL DEFAULT '2408',   -- Impuesto al consumo por pagar

    -- Liquor / alcohol tax (impoconsumo de licores, cervezas, etc.)
    liquor_tax_applicable   BOOLEAN NOT NULL DEFAULT false,
    liquor_tax_rate         NUMERIC(6,4) NOT NULL DEFAULT 0.0000,  -- tenant must set correct rate
    liquor_tax_gl_account_code VARCHAR(20) NOT NULL DEFAULT '2408',

    -- IVA
    iva_applicable          BOOLEAN NOT NULL DEFAULT false,
    iva_rate                NUMERIC(6,4) NOT NULL DEFAULT 0.1900,  -- 19%
    iva_gl_account_code     VARCHAR(20) NOT NULL DEFAULT '2408',   -- IVA por pagar

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(tenant_id)
);

-- Seed one row per existing tenant with all taxes disabled (safe default).
-- Tenants must explicitly enable their applicable tax regime via admin settings.
DO $$
DECLARE
    t_id UUID;
BEGIN
    FOR t_id IN SELECT id FROM tenants LOOP
        INSERT INTO tenant_tax_config (tenant_id)
        VALUES (t_id)
        ON CONFLICT (tenant_id) DO NOTHING;
    END LOOP;
END;
$$;
