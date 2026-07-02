# Tenant Quota Overrides Runbook

Tenant quota overrides are commercial exceptions for plan quotas. They never
delete, deactivate, or mutate tenant resources. They only change the effective
limit used by quota checks and quota usage reporting.

## When To Use

- A paid tenant is already above a new plan limit during rollout.
- A commercial agreement grants a higher limit for one resource.
- Support needs to temporarily disable one quota while a contract is resolved.

Do not change `subscription_plans.features` for one tenant. Global plan quota
metadata must stay plan-wide.

## Resources

Valid `resource` values are:

- `admin_users`
- `active_sessions_per_admin_user`
- `active_kitchens`
- `active_tables_including_bar`
- `active_qr_tables`
- `completed_online_orders_per_month`
- `electronic_invoices_per_period`

## Grant Or Update A Limit Override

Run this in the target environment after replacing the tenant slug, resource,
limit, actor, and reason.

```sql
WITH target AS (
  SELECT id AS tenant_id
  FROM tenants
  WHERE slug = 'tenant-slug'
), previous AS (
  SELECT tq.*
  FROM tenant_quota_overrides tq
  JOIN target t ON t.tenant_id = tq.tenant_id
  WHERE tq.resource = 'active_kitchens'
), upserted AS (
  INSERT INTO tenant_quota_overrides (
    tenant_id, resource, limit_override, disabled, reason, created_by, updated_by
  )
  SELECT
    tenant_id,
    'active_kitchens',
    4,
    false,
    'Commercial exception approved by ops',
    NULL,
    NULL
  FROM target
  ON CONFLICT (tenant_id, resource) DO UPDATE SET
    limit_override = EXCLUDED.limit_override,
    disabled = false,
    reason = EXCLUDED.reason,
    updated_by = EXCLUDED.updated_by,
    updated_at = now()
  RETURNING *
)
INSERT INTO tenant_quota_override_audit (
  tenant_id, resource, action,
  previous_limit_override, new_limit_override,
  previous_disabled, new_disabled,
  reason, actor_user_id
)
SELECT
  u.tenant_id,
  u.resource,
  CASE WHEN p.id IS NULL THEN 'grant' ELSE 'update' END,
  p.limit_override,
  u.limit_override,
  p.disabled,
  u.disabled,
  u.reason,
  u.updated_by
FROM upserted u
LEFT JOIN previous p ON p.tenant_id = u.tenant_id AND p.resource = u.resource;
```

## Disable A Quota For One Tenant

Use `disabled = true` instead of storing `-1`. Application quota coercion treats
negative numbers as `0`, so unlimited overrides must use the boolean flag.

```sql
WITH target AS (
  SELECT id AS tenant_id
  FROM tenants
  WHERE slug = 'tenant-slug'
), previous AS (
  SELECT tq.*
  FROM tenant_quota_overrides tq
  JOIN target t ON t.tenant_id = tq.tenant_id
  WHERE tq.resource = 'completed_online_orders_per_month'
), upserted AS (
  INSERT INTO tenant_quota_overrides (
    tenant_id, resource, limit_override, disabled, reason, created_by, updated_by
  )
  SELECT
    tenant_id,
    'completed_online_orders_per_month',
    NULL,
    true,
    'Temporary unlimited online orders during rollout',
    NULL,
    NULL
  FROM target
  ON CONFLICT (tenant_id, resource) DO UPDATE SET
    limit_override = NULL,
    disabled = true,
    reason = EXCLUDED.reason,
    updated_by = EXCLUDED.updated_by,
    updated_at = now()
  RETURNING *
)
INSERT INTO tenant_quota_override_audit (
  tenant_id, resource, action,
  previous_limit_override, new_limit_override,
  previous_disabled, new_disabled,
  reason, actor_user_id
)
SELECT
  u.tenant_id,
  u.resource,
  CASE WHEN p.id IS NULL THEN 'grant' ELSE 'update' END,
  p.limit_override,
  u.limit_override,
  p.disabled,
  u.disabled,
  u.reason,
  u.updated_by
FROM upserted u
LEFT JOIN previous p ON p.tenant_id = u.tenant_id AND p.resource = u.resource;
```

## Remove An Override

Removing the row returns the tenant to the plan quota. Run the rollout report
first if the tenant may still be above the plan limit.

```sql
WITH target AS (
  SELECT id AS tenant_id
  FROM tenants
  WHERE slug = 'tenant-slug'
), removed AS (
  DELETE FROM tenant_quota_overrides tq
  USING target t
  WHERE tq.tenant_id = t.tenant_id
    AND tq.resource = 'active_kitchens'
  RETURNING tq.*
)
INSERT INTO tenant_quota_override_audit (
  tenant_id, resource, action,
  previous_limit_override, new_limit_override,
  previous_disabled, new_disabled,
  reason, actor_user_id
)
SELECT
  tenant_id,
  resource,
  'remove',
  limit_override,
  NULL,
  disabled,
  NULL,
  'Override removed; tenant returns to plan limit',
  NULL
FROM removed;
```

## Rollout Report

Run the read-only report in `sql/20260701_subscription_plan_quotas.sql` before
enabling or tightening enforcement. The report returns one row per
tenant/resource with:

- `used`
- `plan_limit`
- `effective_limit`
- `override_state`
- `over_plan_limit`
- `over_effective_limit`

Tenants with `over_plan_limit = true` and `over_effective_limit = false` are
protected by an override. Tenants with `over_effective_limit = true` will be
blocked from further growth for that resource.

## Observability

Quota blocks are logged by `billing_service` with tenant, resource, usage,
effective limit, plan limit, plan slug, and override id when present. Use those
logs for support triage before changing a tenant override.
