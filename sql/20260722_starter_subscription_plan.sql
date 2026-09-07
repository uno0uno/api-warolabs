-- api-warolabs#693: permanent Starter plan metadata (non-destructive).
-- Seeds subscription_plans only; does not modify tenants or subscriptions.

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
    'Starter',
    'starter',
    'Plan gratuito permanente — POS mostrador con cuotas operativas limitadas',
    0,
    0,
    10,
    true,
    '{
      "quotas": {
        "admin_users": 1,
        "active_sessions_per_admin_user": 1,
        "active_kitchens": 0,
        "active_tables_including_bar": 0,
        "active_qr_tables": 0,
        "completed_online_orders_per_month": 30,
        "electronic_invoices_per_period": 0
      }
    }'::jsonb
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
    updated_at = now();
