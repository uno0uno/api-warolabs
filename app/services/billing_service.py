"""
Billing Service — scan quota (#58) + MP/Wompi subscriptions (#60) + grace period (#62)

Handles scan quota enforcement, tenant-facing subscription flows, and grace
period access control. Works with tables created in migration #59.

The admin CRUD layer (#61) and its dead `/admin/billing/*` endpoints were
deleted in #185 — no frontend ever consumed them and they posed an RBAC risk.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


def _billing_exempt_tenant_ids() -> set[UUID]:
    """Tenant UUIDs that skip grace-period downgrades (internal/dogfood)."""
    raw = settings.billing_exempt_tenant_ids.strip()
    if not raw:
        return set()
    result: set[UUID] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            result.add(UUID(part))
    return result


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
        # Quota OK — already incremented; log monthly usage
        await _upsert_monthly_log(tenant_id, conn)
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

    # Log monthly usage
    await _upsert_monthly_log(tenant_id, conn)


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


async def _upsert_monthly_log(tenant_id: UUID, conn) -> None:
    """
    Atomically increments the scan_monthly_log counter for the current calendar
    month. Called after every successful scan quota increment.
    Uses ON CONFLICT DO UPDATE so it is safe for concurrent calls.
    """
    sub = await conn.fetchrow("""
        SELECT id FROM tenant_subscriptions
        WHERE tenant_id = $1
          AND status IN ('active', 'past_due')
          AND current_period_end > now()
        ORDER BY current_period_end DESC
        LIMIT 1
    """, tenant_id)

    subscription_id: Optional[UUID] = sub["id"] if sub else None

    await conn.execute("""
        INSERT INTO scan_monthly_log (tenant_id, subscription_id, year_month, scans_count)
        VALUES ($1, $2, DATE_TRUNC('month', NOW())::date, 1)
        ON CONFLICT (tenant_id, year_month)
        DO UPDATE SET scans_count = scan_monthly_log.scans_count + 1
    """, tenant_id, subscription_id)


async def get_scan_monthly_history(tenant_id: UUID, conn, months: int = 12) -> List[Dict[str, Any]]:
    """
    Returns the last N months of scan usage for a tenant from scan_monthly_log.
    Months with zero scans are not included (no row exists).
    """
    rows = await conn.fetch("""
        SELECT year_month, scans_count
        FROM scan_monthly_log
        WHERE tenant_id = $1
        ORDER BY year_month DESC
        LIMIT $2
    """, tenant_id, months)

    return [
        {
            "year_month": row["year_month"].isoformat(),
            "scans_count": row["scans_count"],
        }
        for row in rows
    ]


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


def _serialize_billing_events(rows, total: int, limit: int, offset: int) -> Dict[str, Any]:
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
            "metadata": (json.loads(r["metadata"]) if isinstance(r["metadata"], str) else dict(r["metadata"])) if r["metadata"] else {},
            "created_at": r["created_at"].isoformat(),
        })
    return {"total": total, "limit": limit, "offset": offset, "events": events}


async def list_tenant_billing_events(
    conn, tenant_id, limit: int = 20, offset: int = 0
) -> Dict[str, Any]:
    """Paginated billing events for the session tenant, newest first."""
    rows = await conn.fetch("""
        SELECT
            be.id, be.tenant_id, t.name AS tenant_name,
            be.subscription_id, be.event_type, be.amount,
            be.currency, be.metadata, be.created_at
        FROM billing_events be
        JOIN tenants t ON t.id = be.tenant_id
        WHERE be.tenant_id = $3
        ORDER BY be.created_at DESC
        LIMIT $1 OFFSET $2
    """, limit, offset, tenant_id)
    total = await conn.fetchval(
        "SELECT COUNT(*) FROM billing_events WHERE tenant_id = $1", tenant_id
    )
    return _serialize_billing_events(rows, total, limit, offset)


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
        "features": (json.loads(row["features"]) if isinstance(row["features"], str) else dict(row["features"])) if row["features"] else {},
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


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
    gateway_reference: str,
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
             gateway_reference,
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
            gateway_reference    = EXCLUDED.gateway_reference,
            current_period_start = EXCLUDED.current_period_start,
            current_period_end   = EXCLUDED.current_period_end,
            cancelled_at         = NULL,
            updated_at           = now()
        RETURNING id, tenant_id, plan_id, billing_cycle, status,
                  gateway_reference, current_period_start, current_period_end
    """, tenant_id, plan_id, billing_cycle, gateway_reference)

    sub_id = row["id"]

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, 'subscribe_initiated', $3)
    """, tenant_id, sub_id, json.dumps({"checkout_url": checkout_url, "plan_id": str(plan_id)}))

    logger.info(
        "subscribe_initiated: tenant=%s plan=%s cycle=%s preapproval=%s",
        tenant_id, plan_id, billing_cycle, gateway_reference,
    )

    return {
        "subscription_id": str(sub_id),
        "checkout_url": checkout_url,
        "gateway_reference": gateway_reference,
        "status": "pending",
    }


_ACTIVATABLE_STATUSES = frozenset({"pending", "past_due"})


async def _activate_subscription_with_period(
    conn,
    *,
    subscription_id: UUID,
    tenant_id: UUID,
    billing_cycle: str,
    amount: float,
    currency: str,
    metadata: Dict[str, Any],
) -> Optional[datetime]:
    """Set subscription active, extend billing period, record payment_approved."""
    # Interval literals stay in SQL — asyncpg cannot bind '1 year' strings as interval.
    cycle = billing_cycle if billing_cycle in ("monthly", "annual") else "annual"
    updated = await conn.fetchrow("""
        UPDATE tenant_subscriptions
        SET status               = 'active',
            current_period_start = now(),
            current_period_end   = now() + CASE
                WHEN $2::text = 'monthly' THEN interval '1 month'
                ELSE interval '1 year'
            END,
            updated_at           = now()
        WHERE id = $1
        RETURNING current_period_end
    """, subscription_id, cycle)

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, amount, currency, metadata)
        VALUES ($1, $2, 'payment_approved', $3, $4, $5)
    """, tenant_id, subscription_id, amount, currency, json.dumps(metadata))

    if updated is None:
        return None
    return updated["current_period_end"]


async def activate_subscription_by_gateway_ref(
    conn,
    tenant_id: UUID,
    gateway_reference: str,
    wompi_transaction_id: str,
    amount: float,
) -> None:
    """
    Activa la suscripción del tenant cuando Wompi confirma el pago (verify-payment).
    Extiende el período según billing_cycle para filas pending o past_due.
    """
    row = await conn.fetchrow(
        """SELECT id, status, billing_cycle
           FROM tenant_subscriptions
           WHERE tenant_id = $1 AND gateway_reference = $2""",
        tenant_id, gateway_reference,
    )
    if not row:
        logger.warning(
            "activate_subscription: no subscription found for tenant=%s gateway_ref=%s",
            tenant_id, gateway_reference,
        )
        return

    if row["status"] == "active":
        logger.info("activate_subscription: already active tenant=%s", tenant_id)
        return

    if row["status"] not in _ACTIVATABLE_STATUSES:
        logger.warning(
            "activate_subscription: status=%s not activatable tenant=%s gateway_ref=%s",
            row["status"], tenant_id, gateway_reference,
        )
        return

    period_end = await _activate_subscription_with_period(
        conn,
        subscription_id=row["id"],
        tenant_id=tenant_id,
        billing_cycle=row["billing_cycle"],
        amount=amount,
        currency="COP",
        metadata={
            "wompi_transaction_id": wompi_transaction_id,
            "gateway_reference": gateway_reference,
        },
    )

    logger.info(
        "Subscription activated: tenant=%s transaction=%s amount=%s period_end=%s",
        tenant_id, wompi_transaction_id, amount, period_end,
    )


async def get_tenant_subscription(conn, tenant_id: UUID) -> Dict[str, Any]:
    """
    Return the tenant's current subscription with plan details and current
    period scan usage. Raises 404 if tenant has no subscription row.
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
            ts.gateway_reference,
            ts.cancelled_at,
            ts.created_at,
            ts.updated_at,
            COALESCE(su.scans_used, 0) AS scans_used
        FROM tenant_subscriptions ts
        JOIN tenants t ON t.id = ts.tenant_id
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        LEFT JOIN scan_usage su
            ON su.tenant_id = ts.tenant_id
           AND su.period_start <= now()
           AND su.period_end   >  now()
        WHERE ts.tenant_id = $1
    """, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    data: Dict[str, Any] = {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "tenant_name": row["tenant_name"],
        "plan_id": str(row["plan_id"]),
        "plan_name": row["plan_name"],
        "plan_slug": row["plan_slug"],
        "scan_limit": row["scan_limit"],
        "billing_cycle": row["billing_cycle"],
        "status": row["status"],
        "current_period_start": row["current_period_start"].isoformat(),
        "current_period_end": row["current_period_end"].isoformat(),
        "gateway_reference": row["gateway_reference"],
        "cancelled_at": row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "scans_used": row["scans_used"],
    }
    return data


async def cancel_tenant_subscription(conn, tenant_id: UUID) -> str:
    """
    Set subscription status='cancelled' in DB and return the gateway_reference
    so the caller can cancel it in MP API.
    Raises 404 if no active subscription exists.
    """
    row = await conn.fetchrow("""
        UPDATE tenant_subscriptions
        SET status = 'cancelled', cancelled_at = now(), updated_at = now()
        WHERE tenant_id = $1
          AND status IN ('active', 'pending')
        RETURNING id, gateway_reference
    """, tenant_id)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No hay suscripción activa o pendiente para cancelar",
        )

    sub_id = row["id"]
    gateway_reference = row["gateway_reference"]

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, 'subscription_cancelled', $3)
    """, tenant_id, sub_id, {"gateway_reference": gateway_reference or ""})

    logger.info("subscription_cancelled: tenant=%s preapproval=%s", tenant_id, gateway_reference)

    return gateway_reference or ""


async def activate_tenant_subscription(
    conn,
    gateway_reference: str,
    payment_id: str = "",
    amount: float = 0,
    currency: str = "COP",
):
    """
    Llamado desde el webhook de Wompi cuando la transacción es APPROVED.
    Activa la suscripción y extiende el período según billing_cycle.
    Retorna tenant_info dict o None si no hay fila pending/past_due.
    """
    row = await conn.fetchrow("""
        SELECT ts.id, ts.tenant_id, ts.billing_cycle,
               t.name AS tenant_name, t.email AS tenant_email,
               sp.name AS plan_name
        FROM tenant_subscriptions ts
        JOIN tenants t ON t.id = ts.tenant_id
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        WHERE ts.gateway_reference = $1
          AND ts.status IN ('pending', 'past_due')
    """, gateway_reference)

    if row is None:
        logger.warning(
            "activate_tenant_subscription: no pending/past_due row for gateway_reference=%s",
            gateway_reference,
        )
        return None

    billing_cycle = row["billing_cycle"]
    metadata: Dict[str, Any] = {"gateway_reference": gateway_reference}
    if payment_id:
        metadata["wompi_transaction_id"] = payment_id

    period_end = await _activate_subscription_with_period(
        conn,
        subscription_id=row["id"],
        tenant_id=row["tenant_id"],
        billing_cycle=billing_cycle,
        amount=amount,
        currency=currency,
        metadata=metadata,
    )
    next_period_end = period_end.isoformat() if period_end else None

    logger.info(
        "subscription_activated: tenant=%s gateway_reference=%s cycle=%s",
        row["tenant_id"], gateway_reference, billing_cycle,
    )

    return {
        "tenant_id": str(row["tenant_id"]),
        "subscription_id": str(row["id"]),
        "tenant_name": row["tenant_name"],
        "tenant_email": row["tenant_email"],
        "plan_name": row["plan_name"],
        "next_period_end": next_period_end,
    }


# ── Grace period & access control — issue #62 ────────────────────────────────

GRACE_PERIOD_DAYS = 7
WARNING_THRESHOLD_DAYS = 3


@dataclass
class SubscriptionAccess:
    """
    Represents the access level for a tenant based on subscription status.

    Levels:
      free             — no subscription row; default 1000 scans/month
      full             — active or pending subscription
      full_with_warning — past_due, < 3 days overdue — access OK but banner shown
      read_only        — past_due, 3-7 days overdue — IA scanner blocked
      blocked          — past_due > 7 days OR cancelled/expired
    """
    level: str
    grace_days_remaining: int
    subscription_status: Optional[str]
    next_payment_date: Optional[str]
    message: str


async def get_subscription_access(tenant_id: UUID, conn) -> SubscriptionAccess:
    """
    Returns the access level for a tenant based on their subscription status
    and how many days past_due they are.

    Uses timezone.utc (Python 3.9 safe — NOT datetime.UTC which requires 3.11+).
    """
    if tenant_id in _billing_exempt_tenant_ids():
        return SubscriptionAccess(
            level="full",
            grace_days_remaining=0,
            subscription_status="active",
            next_payment_date=None,
            message="Acceso completo.",
        )

    sub = await conn.fetchrow("""
        SELECT status, current_period_end, plan_id
        FROM tenant_subscriptions
        WHERE tenant_id = $1
    """, tenant_id)

    if sub is None:
        return SubscriptionAccess(
            level="free",
            grace_days_remaining=0,
            subscription_status=None,
            next_payment_date=None,
            message="Estás en el plan gratuito con 1000 escaneos al mes.",
        )

    status = sub["status"]
    period_end = sub["current_period_end"]

    if status in ("active", "pending"):
        return SubscriptionAccess(
            level="full",
            grace_days_remaining=0,
            subscription_status=status,
            next_payment_date=period_end.date().isoformat() if period_end else None,
            message="Acceso completo.",
        )

    if status == "past_due":
        now = datetime.now(timezone.utc)
        # Ensure period_end is timezone-aware for comparison
        if period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)
        days_overdue = max(0, (now - period_end).days)
        grace_remaining = max(0, GRACE_PERIOD_DAYS - days_overdue)

        if days_overdue <= WARNING_THRESHOLD_DAYS:
            return SubscriptionAccess(
                level="full_with_warning",
                grace_days_remaining=grace_remaining,
                subscription_status=status,
                next_payment_date=period_end.date().isoformat(),
                message=(
                    f"Hubo un problema con tu pago. "
                    f"Tienes {grace_remaining} días para renovar antes de perder acceso."
                ),
            )
        elif days_overdue <= GRACE_PERIOD_DAYS:
            return SubscriptionAccess(
                level="read_only",
                grace_days_remaining=grace_remaining,
                subscription_status=status,
                next_payment_date=period_end.date().isoformat(),
                message=(
                    f"Tu acceso a funciones IA está suspendido. "
                    f"Renueva tu suscripción en los próximos {grace_remaining} días."
                ),
            )

    # past_due > 7 days, cancelled, or expired
    return SubscriptionAccess(
        level="blocked",
        grace_days_remaining=0,
        subscription_status=status,
        next_payment_date=None,
        message="Tu suscripción ha vencido. Renueva para recuperar el acceso.",
    )


async def get_past_due_tenants(conn) -> List[Dict[str, Any]]:
    """
    Returns all tenants with past_due subscription and their days overdue.
    Used by the grace reminder cron endpoint.
    """
    rows = await conn.fetch("""
        SELECT
            ts.tenant_id,
            ts.id AS subscription_id,
            ts.current_period_end,
            t.name AS tenant_name,
            t.email AS tenant_email,
            EXTRACT(DAY FROM (now() - ts.current_period_end))::int AS days_overdue
        FROM tenant_subscriptions ts
        JOIN tenants t ON t.id = ts.tenant_id
        WHERE ts.status = 'past_due'
          AND ts.current_period_end < now()
        ORDER BY ts.current_period_end ASC
    """)

    result = []
    for r in rows:
        days = max(0, r["days_overdue"] or 0)
        result.append({
            "tenant_id": str(r["tenant_id"]),
            "subscription_id": str(r["subscription_id"]),
            "tenant_name": r["tenant_name"],
            "tenant_email": r["tenant_email"],
            "days_overdue": days,
            "grace_days_remaining": max(0, GRACE_PERIOD_DAYS - days),
            "period_end": r["current_period_end"].date().isoformat(),
        })
    return result


async def reminder_already_sent(conn, subscription_id: str, days_overdue: int) -> bool:
    """
    Check if a grace reminder email was already sent for this day bucket.
    Prevents duplicate emails when the cron runs multiple times per day.
    """
    # Day buckets: 1, 3, 6, 7
    DAY_BUCKETS = [1, 3, 6, 7]
    # Find the matching bucket
    bucket = next((d for d in sorted(DAY_BUCKETS) if days_overdue <= d), None)
    if bucket is None:
        return True  # > 7 days — no more reminders

    event_type = f"grace_reminder_day_{bucket}"

    row = await conn.fetchrow("""
        SELECT id FROM billing_events
        WHERE subscription_id = $1
          AND event_type = $2
          AND created_at >= now() - interval '20 hours'
    """, subscription_id, event_type)
    return row is not None


async def record_reminder_sent(conn, tenant_id: str, subscription_id: str, days_overdue: int) -> None:
    """Record that a grace reminder was sent in billing_events."""
    DAY_BUCKETS = [1, 3, 6, 7]
    bucket = next((d for d in sorted(DAY_BUCKETS) if days_overdue <= d), days_overdue)
    event_type = f"grace_reminder_day_{bucket}"

    await conn.execute("""
        INSERT INTO billing_events (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, $3, $4)
    """, tenant_id, subscription_id, event_type, {"days_overdue": days_overdue})


# ── Webhook event handlers — issue #63 ───────────────────────────────────────


async def mark_subscription_past_due(
    conn, gateway_reference: str, event_type: str
) -> Optional[Dict[str, Any]]:
    """
    Set subscription status='past_due' for a given preapproval ID.

    Called when MP sends:
    - subscription_preapproval → paused  (event_type='subscription_paused')
    - payment → rejected                 (event_type='payment_rejected')

    Does NOT filter by current status — allows active → past_due transition.
    Returns tenant info dict for email trigger, or None if preapproval not found.
    """
    row = await conn.fetchrow("""
        UPDATE tenant_subscriptions ts
        SET status = 'past_due', updated_at = now()
        WHERE ts.gateway_reference = $1
        RETURNING ts.id AS subscription_id, ts.tenant_id
    """, gateway_reference)

    if row is None:
        logger.warning(
            "mark_subscription_past_due: no subscription for preapproval=%s",
            gateway_reference,
        )
        return None

    sub_id = row["subscription_id"]
    tenant_id = row["tenant_id"]

    # Fetch tenant info for email
    tenant = await conn.fetchrow(
        "SELECT name, email FROM tenants WHERE id = $1", tenant_id
    )

    await conn.execute("""
        INSERT INTO billing_events (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, $3, $4)
    """, tenant_id, sub_id, event_type, {"gateway_reference": gateway_reference})

    logger.info(
        "%s: tenant=%s preapproval=%s",
        event_type, tenant_id, gateway_reference,
    )

    return {
        "tenant_id": str(tenant_id),
        "subscription_id": str(sub_id),
        "tenant_name": tenant["name"] if tenant else "",
        "tenant_email": tenant["email"] if tenant else None,
    }



