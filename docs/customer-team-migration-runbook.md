# Customer/team migration runbook

Issue: api-warolabs#433

This rollout separates customer relationship state from internal team role state.
Customers live in `tenant_customers`; internal platform access remains in
`tenant_members` with one of `superuser`, `admin`, `employee`, `member`, or
`promotor`.

## Preflight

Estimate rows created from legacy customer memberships:

```sql
SELECT tenant_id, count(*) AS legacy_customer_members
FROM tenant_members
WHERE role = 'customer'
  AND COALESCE(is_active, true) = true
  AND terminated_at IS NULL
GROUP BY tenant_id
ORDER BY legacy_customer_members DESC;
```

Estimate customer relationships recoverable from customer-linked activity:

```sql
WITH customer_activity AS (
    SELECT tenant_id, customer_id AS profile_id
    FROM orders
    WHERE customer_id IS NOT NULL
    UNION
    SELECT tenant_id, profile_id FROM customer_wallet_balances
    UNION
    SELECT tenant_id, profile_id FROM customer_wallet_movements
    UNION
    SELECT tenant_id, profile_id FROM waros_wallets
    UNION
    SELECT tenant_id, profile_id FROM waros_transactions
    UNION
    SELECT tenant_id, customer_id AS profile_id
    FROM online_carts
    WHERE customer_id IS NOT NULL
)
SELECT tenant_id, count(*) AS customer_activity_profiles
FROM customer_activity
GROUP BY tenant_id
ORDER BY customer_activity_profiles DESC;
```

Identify active internal sessions that currently resolve to customer-only role:

```sql
SELECT s.tenant_id, count(*) AS active_customer_role_sessions
FROM sessions s
JOIN tenant_members tm
  ON tm.tenant_id = s.tenant_id
 AND tm.user_id = s.user_id
 AND tm.is_active = true
WHERE s.is_active = true
  AND s.expires_at > now()
  AND tm.role = 'customer'
GROUP BY s.tenant_id
ORDER BY active_customer_role_sessions DESC;
```

## Rollout

Apply `sql/20260613_tenant_customers.sql`. It:

- creates `tenant_customers`
- backfills from legacy `tenant_members.role = 'customer'`
- backfills from customer-linked activity tables
- invalidates active customer-role internal sessions with
  `end_reason = 'customer_role_denied'`

## Manual remediation

If a known production customer was already converted from `customer` to an
internal team role before this migration and has no customer-linked activity in
the backfill sources, insert the relationship explicitly:

```sql
INSERT INTO tenant_customers (tenant_id, profile_id, is_active)
VALUES ('<tenant_id>', '<profile_id>', true)
ON CONFLICT (tenant_id, profile_id) DO UPDATE
SET is_active = true,
    updated_at = now();
```

Use this for Karen/Tijuana-style cases only after confirming the `profile.id`
and `tenant.id`.

## Verification

Confirm there are no remaining active customer-role internal sessions:

```sql
SELECT s.id, s.tenant_id, s.user_id, tm.role
FROM sessions s
JOIN tenant_members tm
  ON tm.tenant_id = s.tenant_id
 AND tm.user_id = s.user_id
 AND tm.is_active = true
WHERE s.is_active = true
  AND s.expires_at > now()
  AND tm.role = 'customer';
```

Confirm customer+team coexistence is represented by both tables:

```sql
SELECT tc.tenant_id, tc.profile_id, tm.role
FROM tenant_customers tc
JOIN tenant_members tm
  ON tm.tenant_id = tc.tenant_id
 AND tm.user_id = tc.profile_id
 AND tm.is_active = true
WHERE tc.is_active = true
  AND tm.role = ANY(ARRAY['superuser', 'admin', 'employee', 'member', 'promotor'])
ORDER BY tc.tenant_id, tc.profile_id;
```

Spot-check a known customer+team profile in customer search/detail, Equipo, and
wallet/order metrics. The same `profile.id` should remain visible as a customer
and as an internal team member.

## Auth/session regression note

Issue: api-warolabs#445

The backend owns internal access decisions. Frontend guards may use
`has_internal_access` from `/auth/session` for redirects and UX, but they must
not become the authorization source of truth or depend on a duplicated role
allowlist as the primary decision. When a user is both a customer and an
internal team member, `tenant_customers` preserves the customer relationship and
`tenant_members.role` decides internal platform access.

Regression coverage should keep these cases locked:

- customer-only memberships are denied before an internal session can be used
- hybrid customer+team users with an internal role such as `promotor` are
  allowed into the internal app
- explicit backend allow/deny fields beat frontend role inference, with the
  local team-role list used only as a legacy fallback
