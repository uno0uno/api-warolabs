-- api-warolabs#573: structured plan quota metadata.
-- Non-destructive: this updates global plan metadata only. It does not modify
-- tenants, memberships, sessions, kitchens, tables, carts, orders, or invoices.

UPDATE subscription_plans
SET features = jsonb_set(
        COALESCE(features, '{}'::jsonb),
        '{quotas}',
        '{
          "admin_users": 6,
          "active_sessions_per_admin_user": 1,
          "active_kitchens": 2,
          "active_tables_including_bar": 20,
          "active_qr_tables": 20,
          "completed_online_orders_per_month": 300,
          "electronic_invoices_per_period": 0
        }'::jsonb,
        true
    ),
    updated_at = now()
WHERE slug = 'pro';

UPDATE subscription_plans
SET features = jsonb_set(
        COALESCE(features, '{}'::jsonb),
        '{quotas}',
        '{
          "admin_users": 6,
          "active_sessions_per_admin_user": 1,
          "active_kitchens": 2,
          "active_tables_including_bar": 20,
          "active_qr_tables": 20,
          "completed_online_orders_per_month": 300,
          "electronic_invoices_per_period": 200
        }'::jsonb,
        true
    ),
    updated_at = now()
WHERE slug = 'facturacion-electronica';

-- Manual PR validation: current tenant usage against proposed limits.
-- Run read-only in production/staging and include the over-quota summary in
-- the PR. Customer/buyer memberships are intentionally excluded.
/*
WITH quota_plans AS (
  SELECT id, slug, features->'quotas' AS quotas
  FROM subscription_plans
  WHERE slug IN ('pro', 'facturacion-electronica')
), active_subs AS (
  SELECT DISTINCT ON (ts.tenant_id)
    ts.tenant_id,
    t.slug AS tenant_slug,
    qp.slug AS plan_slug,
    ts.current_period_start,
    ts.current_period_end,
    qp.quotas
  FROM tenant_subscriptions ts
  JOIN tenants t ON t.id = ts.tenant_id
  JOIN quota_plans qp ON qp.id = ts.plan_id
  WHERE ts.status = 'active'
  ORDER BY ts.tenant_id, ts.current_period_end DESC
), usage AS (
  SELECT
    s.tenant_id,
    COUNT(DISTINCT tm.id) FILTER (
      WHERE tm.is_active
        AND tm.role = ANY(ARRAY['superuser','admin','employee','member','promotor']::text[])
    ) AS admin_users,
    COALESCE(MAX(sess.active_sessions), 0) AS active_sessions_per_admin_user,
    COUNT(DISTINCT ks.id) FILTER (WHERE ks.is_active) AS active_kitchens,
    COUNT(DISTINCT tb.id) FILTER (WHERE tb.is_active AND tb.deleted_at IS NULL) AS active_tables_including_bar,
    COUNT(DISTINCT tb.id) FILTER (WHERE tb.is_active AND tb.deleted_at IS NULL AND tb.qr_enabled) AS active_qr_tables,
    COUNT(DISTINCT o.id) FILTER (
      WHERE o.online_cart_id IS NOT NULL
        AND o.status = 'completed'
        AND o.order_date >= s.current_period_start
        AND o.order_date < s.current_period_end
    ) AS completed_online_orders_per_month,
    COUNT(DISTINCT ei.id) FILTER (
      WHERE ei.status = 'accepted'
        AND ei.document_type = 'invoice'
        AND COALESCE(ei.emitted_at, ei.created_at) >= s.current_period_start
        AND COALESCE(ei.emitted_at, ei.created_at) < s.current_period_end
    ) AS electronic_invoices_per_period
  FROM active_subs s
  LEFT JOIN tenant_members tm ON tm.tenant_id = s.tenant_id
  LEFT JOIN LATERAL (
    SELECT COUNT(DISTINCT se.id) AS active_sessions
    FROM sessions se
    WHERE se.tenant_id = s.tenant_id
      AND se.user_id = tm.user_id
      AND se.is_active
      AND se.expires_at > now()
  ) sess ON tm.is_active AND tm.role = ANY(ARRAY['superuser','admin','employee','member','promotor']::text[])
  LEFT JOIN kitchen_stations ks ON ks.tenant_id = s.tenant_id
  LEFT JOIN tables tb ON tb.tenant_id = s.tenant_id
  LEFT JOIN orders o ON o.tenant_id = s.tenant_id
  LEFT JOIN electronic_invoices ei ON ei.tenant_id = s.tenant_id
  GROUP BY s.tenant_id
)
SELECT
  s.tenant_slug,
  s.plan_slug,
  u.*,
  (u.admin_users > COALESCE((s.quotas->>'admin_users')::int, 0)) AS over_admin_users,
  (u.active_sessions_per_admin_user > COALESCE((s.quotas->>'active_sessions_per_admin_user')::int, 0)) AS over_active_sessions_per_admin_user,
  (u.active_kitchens > COALESCE((s.quotas->>'active_kitchens')::int, 0)) AS over_active_kitchens,
  (u.active_tables_including_bar > COALESCE((s.quotas->>'active_tables_including_bar')::int, 0)) AS over_active_tables,
  (u.active_qr_tables > COALESCE((s.quotas->>'active_qr_tables')::int, 0)) AS over_active_qr_tables,
  (u.completed_online_orders_per_month > COALESCE((s.quotas->>'completed_online_orders_per_month')::int, 0)) AS over_online_orders,
  (u.electronic_invoices_per_period > COALESCE((s.quotas->>'electronic_invoices_per_period')::int, 0)) AS over_electronic_invoices
FROM active_subs s
JOIN usage u ON u.tenant_id = s.tenant_id
ORDER BY s.tenant_slug;
*/
