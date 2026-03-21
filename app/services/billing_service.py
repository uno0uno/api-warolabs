"""
Billing Service — scan quota (#58) + admin CRUD (#61) + MP subscriptions (#60)

Handles scan quota enforcement, admin CRUD for billing entities,
and tenant-facing subscription flows (subscribe, cancel, activate via webhook).
Works with tables created in migration #59.
"""
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

logger = logging.getLogger(__name__)


async def check_scan_quota(tenant_id: UUID, conn) -> None:
    """
    Atomic scan quota check and increment.

    Raises HTTP 429 if the tenant has reached their plan's scan limit
    for the current billing period. Creates a scan_usage row on first call.

    Strategy: UPDATE ... WHERE scans_used < scans_limit RETURNING
    If the UPDATE matches 0 rows we check whether it's a quota exceeded
    or a missing row situation — only the latter triggers row creation.
    """
    # Atomic increment — only succeeds if quota not yet reached
    result = await conn.fetchrow("""
        UPDATE scan_usage
        SET
            scans_used      = scans_used + 1,
            last_scanned_at = now(),
            updated_at      = now()
        WHERE tenant_id  = $1
          AND period_start <= now()
          AND period_end   >  now()
          AND scans_used   <  scans_limit
        RETURNING scans_used, scans_limit, period_end
    """, tenant_id)

    if result is not None:
        # Quota OK — already incremented
        return

    # UPDATE matched 0 rows — find out why
    usage = await conn.fetchrow("""
        SELECT scans_used, scans_limit, period_end
        FROM scan_usage
        WHERE tenant_id  = $1
          AND period_start <= now()
          AND period_end   >  now()
    """, tenant_id)

    if usage is not None:
        # Row exists but quota is exhausted
        raise HTTPException(
            status_code=429,
            detail={
                "error": "scan_quota_exceeded",
                "scans_used": usage["scans_used"],
                "scans_limit": usage["scans_limit"],
                "period_end": usage["period_end"].isoformat(),
                "upgrade_url": "/billing/planes",
            },
        )

    # No row for this period → create it then increment
    await _create_period_usage(tenant_id, conn)

    # Re-run the increment now that the row exists
    await conn.execute("""
        UPDATE scan_usage
        SET
            scans_used      = scans_used + 1,
            last_scanned_at = now(),
            updated_at      = now()
        WHERE tenant_id  = $1
          AND period_start <= now()
          AND period_end   >  now()
    """, tenant_id)


async def _create_period_usage(tenant_id: UUID, conn) -> None:
    """
    Creates the scan_usage row for the current calendar month.

    Pulls subscription_id and scan_limit from the tenant's active plan.
    Falls back to 1 000 scans (free default) if no active subscription exists.
    Uses ON CONFLICT DO NOTHING so concurrent first-calls are safe.
    """
    sub = await conn.fetchrow("""
        SELECT ts.id AS subscription_id, sp.scan_limit
        FROM tenant_subscriptions ts
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        WHERE ts.tenant_id = $1
          AND ts.status    = 'active'
          AND ts.current_period_end > now()
        LIMIT 1
    """, tenant_id)

    subscription_id: Optional[UUID] = sub["subscription_id"] if sub else None
    scan_limit: int = sub["scan_limit"] if sub else 1000

    await conn.execute("""
        INSERT INTO scan_usage
            (tenant_id, subscription_id, period_start, period_end,
             scans_used, scans_limit)
        VALUES (
            $1, $2,
            date_trunc('month', now()),
            date_trunc('month', now()) + interval '1 month',
            0,
            $3
        )
        ON CONFLICT (tenant_id, period_start) DO NOTHING
    """, tenant_id, subscription_id, scan_limit)

    logger.info(
        "scan_usage row created: tenant=%s scan_limit=%d", tenant_id, scan_limit
    )


async def get_scan_usage(tenant_id: UUID, conn) -> Dict[str, Any]:
    """
    Returns the current period scan usage for a tenant.

    Falls back to { scans_used: 0, scans_limit: 1000 } when no scan_usage
    row exists yet (tenant has never scanned in this period).
    """
    row = await conn.fetchrow("""
        SELECT scans_used, scans_limit, period_start, period_end
        FROM scan_usage
        WHERE tenant_id  = $1
          AND period_start <= now()
          AND period_end   >  now()
    """, tenant_id)

    if row is None:
        return {
            "scans_used": 0,
            "scans_limit": 1000,
            "period_start": None,
            "period_end": None,
            "percentage": 0.0,
        }

    percentage = (
        round((row["scans_used"] / row["scans_limit"]) * 100, 1)
        if row["scans_limit"] > 0
        else 0.0
    )

    return {
        "scans_used": row["scans_used"],
        "scans_limit": row["scans_limit"],
        "period_start": row["period_start"].date().isoformat(),
        "period_end": row["period_end"].date().isoformat(),
        "percentage": percentage,
    }


# ── Admin CRUD — issue #61 ────────────────────────────────────────────────────

async def list_plans(conn) -> List[Dict[str, Any]]:
    """List all subscription plans ordered by monthly price."""
    rows = await conn.fetch("""
        SELECT id, name, slug, description, price_monthly, price_annual,
               scan_limit, is_active, features, created_at, updated_at
        FROM subscription_plans
        ORDER BY price_monthly ASC
    """)
    return [_serialize_plan(r) for r in rows]


async def create_plan(conn, data: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a new subscription plan. Raises 409 if slug already exists."""
    existing = await conn.fetchrow(
        "SELECT id FROM subscription_plans WHERE slug = $1", data["slug"]
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail={"error": "slug_conflict", "slug": data["slug"]},
        )

    row = await conn.fetchrow("""
        INSERT INTO subscription_plans
            (name, slug, description, price_monthly, price_annual, scan_limit, features)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, name, slug, description, price_monthly, price_annual,
                  scan_limit, is_active, features, created_at, updated_at
    """,
        data["name"],
        data["slug"],
        data.get("description"),
        data["price_monthly"],
        data["price_annual"],
        data.get("scan_limit", 1000),
        data.get("features", {}),
    )
    logger.info("subscription_plan created: slug=%s id=%s", data["slug"], row["id"])
    return _serialize_plan(row)


async def update_plan(
    conn, plan_id: UUID, data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Update mutable fields on a subscription plan.
    Only updates fields that are present in `data` (partial update).
    Raises 404 if plan not found.
    """
    row = await conn.fetchrow(
        "SELECT * FROM subscription_plans WHERE id = $1", plan_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    updated = await conn.fetchrow("""
        UPDATE subscription_plans SET
            name          = COALESCE($2, name),
            description   = COALESCE($3, description),
            price_monthly = COALESCE($4, price_monthly),
            price_annual  = COALESCE($5, price_annual),
            scan_limit    = COALESCE($6, scan_limit),
            features      = COALESCE($7, features),
            updated_at    = now()
        WHERE id = $1
        RETURNING id, name, slug, description, price_monthly, price_annual,
                  scan_limit, is_active, features, created_at, updated_at
    """,
        plan_id,
        data.get("name"),
        data.get("description"),
        data.get("price_monthly"),
        data.get("price_annual"),
        data.get("scan_limit"),
        data.get("features"),
    )
    return _serialize_plan(updated)


async def deactivate_plan(conn, plan_id: UUID) -> Dict[str, Any]:
    """
    Soft-delete a subscription plan (is_active = false).
    Raises 404 if plan not found.
    """
    row = await conn.fetchrow("""
        UPDATE subscription_plans
        SET is_active = false, updated_at = now()
        WHERE id = $1
        RETURNING id, name, slug, is_active
    """, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return {"id": str(row["id"]), "name": row["name"], "slug": row["slug"], "is_active": row["is_active"]}


async def list_subscriptions(conn) -> List[Dict[str, Any]]:
    """List all tenant subscriptions with tenant name and plan name."""
    rows = await conn.fetch("""
        SELECT
            ts.id,
            ts.tenant_id,
            t.name AS tenant_name,
            ts.plan_id,
            sp.name AS plan_name,
            sp.slug AS plan_slug,
            ts.billing_cycle,
            ts.status,
            ts.current_period_start,
            ts.current_period_end,
            ts.mp_subscription_id,
            ts.cancelled_at,
            ts.created_at,
            ts.updated_at
        FROM tenant_subscriptions ts
        JOIN tenants t ON t.id = ts.tenant_id
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        ORDER BY ts.created_at DESC
    """)
    return [_serialize_subscription(r) for r in rows]


async def get_subscription_by_tenant(
    conn, tenant_id: UUID
) -> Dict[str, Any]:
    """
    Get a single tenant's subscription with plan details.
    Raises 404 if tenant has no subscription.
    """
    row = await conn.fetchrow("""
        SELECT
            ts.id,
            ts.tenant_id,
            t.name AS tenant_name,
            ts.plan_id,
            sp.name AS plan_name,
            sp.slug AS plan_slug,
            sp.scan_limit,
            ts.billing_cycle,
            ts.status,
            ts.current_period_start,
            ts.current_period_end,
            ts.mp_subscription_id,
            ts.mp_preapproval_id,
            ts.cancelled_at,
            ts.created_at,
            ts.updated_at
        FROM tenant_subscriptions ts
        JOIN tenants t ON t.id = ts.tenant_id
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        WHERE ts.tenant_id = $1
    """, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _serialize_subscription(row)


VALID_SUBSCRIPTION_STATUSES = {"pending", "active", "past_due", "cancelled", "expired"}


async def update_subscription(
    conn, sub_id: UUID, data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Manually update a tenant subscription's status and/or plan.
    Validates status against the CHECK constraint values.
    Raises 404 if subscription not found, 422 if status is invalid.
    """
    if "status" in data and data["status"] not in VALID_SUBSCRIPTION_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_status",
                "allowed": sorted(VALID_SUBSCRIPTION_STATUSES),
            },
        )

    row = await conn.fetchrow(
        "SELECT id FROM tenant_subscriptions WHERE id = $1", sub_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    updated = await conn.fetchrow("""
        UPDATE tenant_subscriptions SET
            status     = COALESCE($2, status),
            plan_id    = COALESCE($3, plan_id),
            updated_at = now()
        WHERE id = $1
        RETURNING id, tenant_id, plan_id, billing_cycle, status,
                  current_period_start, current_period_end, updated_at
    """,
        sub_id,
        data.get("status"),
        data.get("plan_id"),
    )
    return {
        "id": str(updated["id"]),
        "tenant_id": str(updated["tenant_id"]),
        "plan_id": str(updated["plan_id"]),
        "billing_cycle": updated["billing_cycle"],
        "status": updated["status"],
        "current_period_start": updated["current_period_start"].isoformat(),
        "current_period_end": updated["current_period_end"].isoformat(),
        "updated_at": updated["updated_at"].isoformat(),
    }


async def list_usage_summary(conn) -> List[Dict[str, Any]]:
    """
    Returns scan usage for the current period for all tenants.
    Tenants with no scan_usage row appear with scans_used=0.
    """
    rows = await conn.fetch("""
        SELECT
            t.id AS tenant_id,
            t.name AS tenant_name,
            sp.name AS plan_name,
            sp.slug AS plan_slug,
            COALESCE(su.scans_used, 0) AS scans_used,
            COALESCE(su.scans_limit, sp.scan_limit) AS scans_limit,
            su.last_scanned_at,
            su.period_start,
            su.period_end
        FROM tenants t
        JOIN tenant_subscriptions ts ON ts.tenant_id = t.id
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        LEFT JOIN scan_usage su
            ON su.tenant_id = t.id
           AND su.period_start <= now()
           AND su.period_end   >  now()
        ORDER BY scans_used DESC, t.name ASC
    """)

    result = []
    for r in rows:
        scans_used = r["scans_used"]
        scans_limit = r["scans_limit"]
        percentage = (
            round((scans_used / scans_limit) * 100, 1)
            if scans_limit > 0
            else 0.0
        )
        result.append({
            "tenant_id": str(r["tenant_id"]),
            "tenant_name": r["tenant_name"],
            "plan_name": r["plan_name"],
            "plan_slug": r["plan_slug"],
            "scans_used": scans_used,
            "scans_limit": scans_limit,
            "percentage": percentage,
            "last_scanned_at": r["last_scanned_at"].isoformat() if r["last_scanned_at"] else None,
            "period_start": r["period_start"].date().isoformat() if r["period_start"] else None,
            "period_end": r["period_end"].date().isoformat() if r["period_end"] else None,
        })
    return result


async def list_billing_events(
    conn, limit: int = 50, offset: int = 0
) -> Dict[str, Any]:
    """Paginated billing events log, newest first."""
    rows = await conn.fetch("""
        SELECT
            be.id,
            be.tenant_id,
            t.name AS tenant_name,
            be.subscription_id,
            be.event_type,
            be.amount,
            be.currency,
            be.mp_payment_id,
            be.metadata,
            be.created_at
        FROM billing_events be
        JOIN tenants t ON t.id = be.tenant_id
        ORDER BY be.created_at DESC
        LIMIT $1 OFFSET $2
    """, limit, offset)

    total = await conn.fetchval("SELECT COUNT(*) FROM billing_events")

    events = []
    for r in rows:
        events.append({
            "id": str(r["id"]),
            "tenant_id": str(r["tenant_id"]),
            "tenant_name": r["tenant_name"],
            "subscription_id": str(r["subscription_id"]) if r["subscription_id"] else None,
            "event_type": r["event_type"],
            "amount": str(r["amount"]) if r["amount"] is not None else None,
            "currency": r["currency"],
            "mp_payment_id": r["mp_payment_id"],
            "metadata": dict(r["metadata"]) if r["metadata"] else {},
            "created_at": r["created_at"].isoformat(),
        })

    return {"total": total, "limit": limit, "offset": offset, "events": events}


# ── Serialization helpers ─────────────────────────────────────────────────────

def _serialize_plan(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "price_monthly": str(row["price_monthly"]),
        "price_annual": str(row["price_annual"]),
        "scan_limit": row["scan_limit"],
        "is_active": row["is_active"],
        "features": dict(row["features"]) if row["features"] else {},
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _serialize_subscription(row) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "tenant_name": row["tenant_name"],
        "plan_id": str(row["plan_id"]),
        "plan_name": row["plan_name"],
        "plan_slug": row["plan_slug"],
        "billing_cycle": row["billing_cycle"],
        "status": row["status"],
        "current_period_start": row["current_period_start"].isoformat(),
        "current_period_end": row["current_period_end"].isoformat(),
        "mp_subscription_id": row["mp_subscription_id"],
        "cancelled_at": row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
    # Optional fields (not always selected)
    if "mp_preapproval_id" in row.keys():
        data["mp_preapproval_id"] = row["mp_preapproval_id"]
    if "scan_limit" in row.keys():
        data["scan_limit"] = row["scan_limit"]
    return data


# ── Tenant subscription flows — issue #60 ────────────────────────────────────

async def get_tenant_email(conn, tenant_id: UUID) -> Optional[str]:
    """Return the email for a tenant, or None if not set."""
    row = await conn.fetchrow(
        "SELECT email FROM tenants WHERE id = $1", tenant_id
    )
    return row["email"] if row else None


async def get_plan_for_subscribe(conn, plan_id: UUID) -> Dict[str, Any]:
    """
    Return plan data needed to create a MP preapproval.
    Raises 404 if plan not found or is inactive.
    """
    row = await conn.fetchrow("""
        SELECT id, name, price_monthly, price_annual, scan_limit
        FROM subscription_plans
        WHERE id = $1 AND is_active = true
    """, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado o inactivo")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "price_monthly": float(row["price_monthly"]),
        "price_annual": float(row["price_annual"]),
        "scan_limit": row["scan_limit"],
    }


async def subscribe_tenant(
    conn,
    tenant_id: UUID,
    plan_id: UUID,
    billing_cycle: str,
    checkout_url: str,
    mp_preapproval_id: str,
) -> Dict[str, Any]:
    """
    Update (or insert) the tenant's subscription row with the new plan and
    MP preapproval ID, setting status='pending' until the webhook confirms.
    Also inserts a billing_events row for auditability.

    Uses INSERT ... ON CONFLICT (tenant_id) DO UPDATE to handle both
    first-time tenants (no seed row) and existing ones.
    """
    row = await conn.fetchrow("""
        INSERT INTO tenant_subscriptions
            (tenant_id, plan_id, billing_cycle, status,
             mp_preapproval_id,
             current_period_start, current_period_end)
        VALUES (
            $1, $2, $3, 'pending',
            $4,
            date_trunc('month', now()),
            date_trunc('month', now()) + interval '1 month'
        )
        ON CONFLICT (tenant_id) DO UPDATE SET
            plan_id              = EXCLUDED.plan_id,
            billing_cycle        = EXCLUDED.billing_cycle,
            status               = 'pending',
            mp_preapproval_id    = EXCLUDED.mp_preapproval_id,
            current_period_start = EXCLUDED.current_period_start,
            current_period_end   = EXCLUDED.current_period_end,
            cancelled_at         = NULL,
            updated_at           = now()
        RETURNING id, tenant_id, plan_id, billing_cycle, status,
                  mp_preapproval_id, current_period_start, current_period_end
    """, tenant_id, plan_id, billing_cycle, mp_preapproval_id)

    sub_id = row["id"]

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, 'subscribe_initiated', $3)
    """, tenant_id, sub_id, {"checkout_url": checkout_url, "plan_id": str(plan_id)})

    logger.info(
        "subscribe_initiated: tenant=%s plan=%s cycle=%s preapproval=%s",
        tenant_id, plan_id, billing_cycle, mp_preapproval_id,
    )

    return {
        "subscription_id": str(sub_id),
        "checkout_url": checkout_url,
        "mp_preapproval_id": mp_preapproval_id,
        "status": "pending",
    }


async def get_tenant_subscription(conn, tenant_id: UUID) -> Dict[str, Any]:
    """
    Return the tenant's current subscription with plan details.
    Raises 404 if tenant has no subscription row.
    """
    return await get_subscription_by_tenant(conn, tenant_id)


async def cancel_tenant_subscription(conn, tenant_id: UUID) -> str:
    """
    Set subscription status='cancelled' in DB and return the mp_preapproval_id
    so the caller can cancel it in MP API.
    Raises 404 if no active subscription exists.
    """
    row = await conn.fetchrow("""
        UPDATE tenant_subscriptions
        SET status = 'cancelled', cancelled_at = now(), updated_at = now()
        WHERE tenant_id = $1
          AND status IN ('active', 'pending')
        RETURNING id, mp_preapproval_id
    """, tenant_id)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No hay suscripción activa o pendiente para cancelar",
        )

    sub_id = row["id"]
    mp_preapproval_id = row["mp_preapproval_id"]

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, 'subscription_cancelled', $3)
    """, tenant_id, sub_id, {"mp_preapproval_id": mp_preapproval_id or ""})

    logger.info("subscription_cancelled: tenant=%s preapproval=%s", tenant_id, mp_preapproval_id)

    return mp_preapproval_id or ""


async def activate_tenant_subscription(conn, mp_preapproval_id: str) -> bool:
    """
    Called from the MP webhook when status='authorized'.
    Sets status='active' for the subscription with the given preapproval ID.
    Returns True if a row was updated, False if no matching row was found.
    """
    row = await conn.fetchrow("""
        UPDATE tenant_subscriptions
        SET status = 'active', updated_at = now()
        WHERE mp_preapproval_id = $1
          AND status = 'pending'
        RETURNING id, tenant_id
    """, mp_preapproval_id)

    if row is None:
        logger.warning(
            "activate_tenant_subscription: no pending row for preapproval=%s",
            mp_preapproval_id,
        )
        return False

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, 'subscription_activated', $3)
    """, row["tenant_id"], row["id"], {"mp_preapproval_id": mp_preapproval_id})

    logger.info(
        "subscription_activated: tenant=%s preapproval=%s",
        row["tenant_id"], mp_preapproval_id,
    )
    return True
