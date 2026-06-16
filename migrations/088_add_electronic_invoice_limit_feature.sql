-- Migration 088: store electronic invoice entitlement as structured metadata
--
-- 087 added the plan, but existing environments may already have run it before
-- the numeric entitlement key existed. Keep this idempotent.

UPDATE subscription_plans
SET
    features = jsonb_set(
        COALESCE(features, '{}'::jsonb),
        '{electronic_invoice_limit}',
        '200'::jsonb,
        true
    ),
    updated_at = now()
WHERE slug = 'facturacion-electronica';
