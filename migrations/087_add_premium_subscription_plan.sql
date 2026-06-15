-- Migration 087: add annual electronic invoicing subscription plan
--
-- Tenant-facing plans are read from subscription_plans by /billing/plans.
-- Keep this idempotent so it can be applied safely across environments.

WITH upserted_plan AS (
INSERT INTO subscription_plans (
    name,
    slug,
    description,
    price_monthly,
    price_annual,
    scan_limit,
    is_active,
    features
)
VALUES (
    'Facturación electrónica',
    'facturacion-electronica',
    'Acceso completo a WARO con módulo de Facturación Electrónica y firma digital incluida.',
    20000,
    200000,
    500,
    true,
    jsonb_build_object(
        'electronic_invoices', '200 facturas electrónicas incluidas',
        'digital_signature', 'Firma digital incluida vía Matías API',
        'cost_control', 'Control de costos en tiempo real',
        'profitability', 'Análisis de rentabilidad por plato',
        'invoice_ai', 'Escaneo inteligente de facturas',
        'support', 'Respuesta en menos de 24 horas'
    )
)
ON CONFLICT (slug) DO UPDATE
SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    price_monthly = EXCLUDED.price_monthly,
    price_annual = EXCLUDED.price_annual,
    scan_limit = EXCLUDED.scan_limit,
    is_active = EXCLUDED.is_active,
    features = EXCLUDED.features,
    updated_at = now()
RETURNING id
)
DELETE FROM subscription_plans
WHERE slug = 'premium'
  AND NOT EXISTS (
      SELECT 1
      FROM tenant_subscriptions ts
      WHERE ts.plan_id = subscription_plans.id
  )
  AND id NOT IN (SELECT id FROM upserted_plan);
