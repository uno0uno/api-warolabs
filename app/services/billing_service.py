"""
Billing Service — scan quota management (issue #58)

Handles scan quota enforcement for the AI invoice scanner endpoint.
Works with scan_usage and tenant_subscriptions tables created in migration #59.
"""
import logging
from typing import Any, Dict, Optional
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
