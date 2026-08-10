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
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from app.core.exceptions import APIError
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.services import onboarding_service

logger = logging.getLogger(__name__)

ELECTRONIC_INVOICE_PLAN_SLUG = "facturacion-electronica"
STARTER_PLAN_SLUG = "starter"
STARTER_SCAN_LIMIT = 10
ELECTRONIC_INVOICE_PERIOD_LIMIT = 200
ELECTRONIC_INVOICE_LIMIT_FEATURE = "electronic_invoice_limit"
QUOTAS_FEATURE = "quotas"
STARTER_OPERATIONAL_QUOTAS = {
    "admin_users": 1,
    "active_sessions_per_admin_user": 1,
    "active_kitchens": 0,
    "active_tables_including_bar": 0,
    "active_qr_tables": 0,
    "completed_online_orders_per_month": 30,
    "electronic_invoices_per_period": 0,
    "menu_products": 10,
    "menu_categories": 5,
    "tenant_ingredients": 5,
    "tenant_suppliers": 3,
    "direct_purchases_per_period": 15,
    "stock_adjustments_per_period": 20,
    "cash_closes_per_period": 30,
    "active_open_cash_shifts": 1,
    "expenses_per_period": 30,
    "supplier_payments_per_period": 30,
    "expense_payments_per_period": 30,
    "payment_methods": 5,
    "api_tokens": 0,
    "tenant_promotions": 1,
    "accounting_period_closes_per_period": 3,
    "manual_journal_entries_per_period": 30,
    "modifier_groups": 4,
    "recipe_bases": 5,
    "recipe_lines_per_product": 4,
    "modifier_options_per_group": 6,
    "recipe_base_template_lines": 4,
}
QUOTA_KEYS = (
    "admin_users",
    "active_sessions_per_admin_user",
    "active_kitchens",
    "active_tables_including_bar",
    "active_qr_tables",
    "completed_online_orders_per_month",
    "electronic_invoices_per_period",
    "menu_products",
    "menu_categories",
    "tenant_ingredients",
    "tenant_suppliers",
    "direct_purchases_per_period",
    "stock_adjustments_per_period",
    "cash_closes_per_period",
    "active_open_cash_shifts",
    "expenses_per_period",
    "supplier_payments_per_period",
    "expense_payments_per_period",
    "payment_methods",
    "api_tokens",
    "tenant_promotions",
    "accounting_period_closes_per_period",
    "manual_journal_entries_per_period",
    "modifier_groups",
    "recipe_bases",
    "recipe_lines_per_product",
    "modifier_options_per_group",
    "recipe_base_template_lines",
)
CATALOG_UNLIMITED = 1_000_000
BASE_OPERATIONAL_QUOTAS = {
    "admin_users": 6,
    "active_sessions_per_admin_user": 1,
    "active_kitchens": 2,
    "active_tables_including_bar": 20,
    "active_qr_tables": 20,
    "completed_online_orders_per_month": 300,
    "menu_products": CATALOG_UNLIMITED,
    "menu_categories": CATALOG_UNLIMITED,
    "tenant_ingredients": CATALOG_UNLIMITED,
    "tenant_suppliers": CATALOG_UNLIMITED,
    "direct_purchases_per_period": CATALOG_UNLIMITED,
    "stock_adjustments_per_period": CATALOG_UNLIMITED,
    "cash_closes_per_period": CATALOG_UNLIMITED,
    "active_open_cash_shifts": CATALOG_UNLIMITED,
    "expenses_per_period": CATALOG_UNLIMITED,
    "supplier_payments_per_period": CATALOG_UNLIMITED,
    "expense_payments_per_period": CATALOG_UNLIMITED,
    "payment_methods": CATALOG_UNLIMITED,
    "api_tokens": CATALOG_UNLIMITED,
    "tenant_promotions": CATALOG_UNLIMITED,
    "accounting_period_closes_per_period": CATALOG_UNLIMITED,
    "manual_journal_entries_per_period": CATALOG_UNLIMITED,
    "modifier_groups": CATALOG_UNLIMITED,
    "recipe_bases": CATALOG_UNLIMITED,
    "recipe_lines_per_product": 100,
    "modifier_options_per_group": 50,
    "recipe_base_template_lines": 75,
}
PLAN_QUOTA_DEFAULTS = {
    STARTER_PLAN_SLUG: {
        **STARTER_OPERATIONAL_QUOTAS,
    },
    "pro": {
        **BASE_OPERATIONAL_QUOTAS,
        "electronic_invoices_per_period": 0,
    },
    ELECTRONIC_INVOICE_PLAN_SLUG: {
        **BASE_OPERATIONAL_QUOTAS,
        "electronic_invoices_per_period": ELECTRONIC_INVOICE_PERIOD_LIMIT,
    },
}

QUOTA_UPGRADE_URL = "/billing/planes"
QUOTA_CONTACT_MESSAGE = "Actualiza tu plan o contacta a soporte para ampliar este límite."
ONLINE_ORDER_QUOTA_CUSTOMER_MESSAGE = (
    "El restaurante no puede recibir más pedidos en línea por este periodo. "
    "Intenta contactarlo directamente."
)
ONLINE_ORDER_QUOTA_RESOURCE = "completed_online_orders_per_month"
ENFORCEABLE_QUOTA_RESOURCES = {
    "admin_users",
    "active_kitchens",
    "active_tables_including_bar",
    "active_qr_tables",
    "menu_products",
    "menu_categories",
    "tenant_ingredients",
    "tenant_suppliers",
    "active_open_cash_shifts",
    "payment_methods",
    "api_tokens",
    "tenant_promotions",
    "modifier_groups",
    "recipe_bases",
}
PERIOD_QUOTA_RESOURCES = {
    "direct_purchases_per_period",
    "stock_adjustments_per_period",
    "cash_closes_per_period",
    "expenses_per_period",
    "supplier_payments_per_period",
    "expense_payments_per_period",
    "accounting_period_closes_per_period",
    "manual_journal_entries_per_period",
}
SCOPED_QUOTA_RESOURCES = {
    "recipe_lines_per_product",
    "modifier_options_per_group",
    "recipe_base_template_lines",
}


@dataclass(frozen=True)
class EffectiveQuota:
    resource: str
    plan_limit: int
    limit: Optional[int]
    override_id: Optional[UUID] = None
    override_disabled: bool = False
    override_reason: Optional[str] = None

    @property
    def has_override(self) -> bool:
        return self.override_id is not None


@dataclass(frozen=True)
class OnlineOrderQuotaState:
    plan_slug: str
    quota: EffectiveQuota
    period_start: datetime
    period_end: datetime
    used: int


@dataclass(frozen=True)
class PeriodQuotaState:
    plan_slug: str
    quota: EffectiveQuota
    period_start: datetime
    period_end: datetime
    used: int


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


async def get_effective_plan_slug(conn, tenant_id: UUID) -> Optional[str]:
    """
    Paid subscription wins. Otherwise Starter applies unless onboarding is still
    waiting on payment (legacy paid-first path).
    """
    paid = await conn.fetchrow(
        """
        SELECT sp.slug AS plan_slug
        FROM tenant_subscriptions ts
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        WHERE ts.tenant_id = $1
          AND ts.status IN ('active', 'past_due')
          AND ts.current_period_end > now()
        ORDER BY ts.current_period_end DESC
        LIMIT 1
        """,
        tenant_id,
    )
    if paid:
        return paid["plan_slug"]

    onboarding_state = await conn.fetchval(
        "SELECT state FROM tenant_onboarding WHERE tenant_id = $1",
        tenant_id,
    )
    if onboarding_state == "payment_pending":
        return None

    return STARTER_PLAN_SLUG


async def get_effective_plan_quotas(conn, tenant_id: UUID) -> Dict[str, int]:
    plan_slug = await get_effective_plan_slug(conn, tenant_id)
    if not plan_slug:
        plan_slug = STARTER_PLAN_SLUG
    row = await conn.fetchrow(
        """
        SELECT features
        FROM subscription_plans
        WHERE slug = $1
          AND is_active = true
        LIMIT 1
        """,
        plan_slug,
    )
    features = row["features"] if row else {}
    return _normalize_plan_quotas(plan_slug, features)


STARTER_PLAN_MODULE_VALUES = frozenset({
    "pos",
    "ventas",
    "despacho",
    "menu",
    "operaciones",
    "abastecimiento",
    "analitica",
    "crm",
    "finanzas",
    "integraciones",
    "equipo",
    "facturacion",
    "mi_negocio",
    "mi_plan",
})
STARTER_LOCKED_OPERATION_TOGGLES = frozenset({
    "tables_enabled",
    "comandas_enabled",
    "kds_enabled",
})


async def is_starter_plan(conn, tenant_id: UUID) -> bool:
    return await get_effective_plan_slug(conn, tenant_id) == STARTER_PLAN_SLUG


async def assert_starter_toggle_allowed(conn, tenant_id: UUID, column_name: str, enabled: bool) -> None:
    if not enabled or column_name not in STARTER_LOCKED_OPERATION_TOGGLES:
        return
    if await is_starter_plan(conn, tenant_id):
        raise APIError(
            "Función no disponible en el plan Starter",
            status_code=403,
            details={
                "code": "starter_plan_restriction",
                "toggle": column_name,
                "upgrade_url": QUOTA_UPGRADE_URL,
                "message": QUOTA_CONTACT_MESSAGE,
            },
        )


async def assert_starter_shift_template_growth_allowed(conn, tenant_id: UUID) -> None:
    """Block Starter create/reactivate of Operaciones shift templates (warocol.com#1916)."""
    if await is_starter_plan(conn, tenant_id):
        raise APIError(
            "Función no disponible en el plan Starter",
            status_code=403,
            details={
                "code": "starter_plan_restriction",
                "feature": "shift_templates",
                "upgrade_url": QUOTA_UPGRADE_URL,
                "message": QUOTA_CONTACT_MESSAGE,
            },
        )


async def _default_scan_limit_for_tenant(conn, tenant_id: UUID) -> int:
    plan_slug = await get_effective_plan_slug(conn, tenant_id)
    if plan_slug == STARTER_PLAN_SLUG:
        row = await conn.fetchrow(
            """
            SELECT scan_limit
            FROM subscription_plans
            WHERE slug = $1
              AND is_active = true
            LIMIT 1
            """,
            STARTER_PLAN_SLUG,
        )
        if row and row["scan_limit"] is not None:
            return int(row["scan_limit"])
        return STARTER_SCAN_LIMIT
    return 1000


async def _fetch_plan_quota_context(conn, tenant_id: UUID, resource: str):
    plan = await conn.fetchrow(
        """
        SELECT
            sp.slug AS plan_slug,
            sp.features AS plan_features,
            tq.id AS override_id,
            tq.limit_override,
            COALESCE(tq.disabled, false) AS override_disabled,
            tq.reason AS override_reason
        FROM tenant_subscriptions ts
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        LEFT JOIN tenant_quota_overrides tq
          ON tq.tenant_id = ts.tenant_id
         AND tq.resource = $2
        WHERE ts.tenant_id = $1
          AND ts.status IN ('active', 'past_due')
          AND ts.current_period_end > now()
        ORDER BY ts.current_period_end DESC
        LIMIT 1
        """,
        tenant_id,
        resource,
    )
    if plan:
        return plan

    effective_slug = await get_effective_plan_slug(conn, tenant_id)
    if effective_slug != STARTER_PLAN_SLUG:
        return None

    return await conn.fetchrow(
        """
        SELECT
            sp.slug AS plan_slug,
            sp.features AS plan_features,
            tq.id AS override_id,
            tq.limit_override,
            COALESCE(tq.disabled, false) AS override_disabled,
            tq.reason AS override_reason
        FROM subscription_plans sp
        LEFT JOIN tenant_quota_overrides tq
          ON tq.tenant_id = $1
         AND tq.resource = $2
        WHERE sp.slug = $3
          AND sp.is_active = true
        LIMIT 1
        """,
        tenant_id,
        resource,
        STARTER_PLAN_SLUG,
    )


async def check_plan_quota_growth(
    conn,
    tenant_id: UUID,
    resource: str,
    *,
    exclude_pending_invitation_id: Optional[UUID] = None,
) -> None:
    """
    Block active resource growth once the tenant reaches the plan quota.

    This is intentionally non-destructive: it never modifies existing rows and
    should be called only before create/reactivate/enable transitions.
    """
    if resource not in ENFORCEABLE_QUOTA_RESOURCES:
        raise ValueError(f"Unsupported quota resource: {resource}")

    plan = await _fetch_plan_quota_context(conn, tenant_id, resource)
    if not plan:
        return

    quotas = _normalize_plan_quotas(plan["plan_slug"], plan["plan_features"])
    quota = _effective_quota_from_row(resource, quotas.get(resource, 0), plan)
    if quota.limit is None:
        return

    used = await _count_quota_resource_usage(
        conn,
        tenant_id,
        resource,
        exclude_pending_invitation_id=exclude_pending_invitation_id,
    )
    if used < quota.limit:
        return

    _log_quota_block(
        tenant_id=tenant_id,
        resource=resource,
        used=used,
        quota=quota,
        plan_slug=plan["plan_slug"],
    )
    raise APIError(
        "Límite del plan alcanzado",
        status_code=429,
        details={
            "code": "quota_exceeded",
            "error": "quota_exceeded",
            "resource": resource,
            "used": used,
            "limit": quota.limit,
            "plan_limit": quota.plan_limit,
            "plan_slug": plan["plan_slug"],
            "override": _quota_override_payload(quota),
            "upgrade_url": QUOTA_UPGRADE_URL,
            "message": QUOTA_CONTACT_MESSAGE,
        },
    )


async def check_plan_quota_scoped(
    conn,
    tenant_id: UUID,
    resource: str,
    scope_id: UUID,
    *,
    projected_count: Optional[int] = None,
) -> None:
    """Block scoped resource growth (per product/group/template)."""
    if resource not in SCOPED_QUOTA_RESOURCES:
        raise ValueError(f"Unsupported scoped quota resource: {resource}")

    plan = await _fetch_plan_quota_context(conn, tenant_id, resource)
    if not plan:
        return

    quotas = _normalize_plan_quotas(plan["plan_slug"], plan["plan_features"])
    quota = _effective_quota_from_row(resource, quotas.get(resource, 0), plan)
    if quota.limit is None:
        return

    if projected_count is not None:
        used = projected_count
    else:
        used = await _count_scoped_quota_usage(conn, resource, scope_id) + 1

    if used <= quota.limit:
        return

    _log_quota_block(
        tenant_id=tenant_id,
        resource=resource,
        used=used,
        quota=quota,
        plan_slug=plan["plan_slug"],
    )
    raise APIError(
        "Límite del plan alcanzado",
        status_code=429,
        details={
            "code": "quota_exceeded",
            "error": "quota_exceeded",
            "resource": resource,
            "scope_id": str(scope_id),
            "used": used,
            "limit": quota.limit,
            "plan_limit": quota.plan_limit,
            "plan_slug": plan["plan_slug"],
            "override": _quota_override_payload(quota),
            "upgrade_url": QUOTA_UPGRADE_URL,
            "message": QUOTA_CONTACT_MESSAGE,
        },
    )


async def _fetch_period_quota_context(conn, tenant_id: UUID, resource: str):
    """Plan + billing-period window for period-based operational quotas."""
    plan = await conn.fetchrow(
        """
        SELECT
            sp.slug AS plan_slug,
            sp.features AS plan_features,
            ts.current_period_start,
            ts.current_period_end,
            tq.id AS override_id,
            tq.limit_override,
            COALESCE(tq.disabled, false) AS override_disabled,
            tq.reason AS override_reason
        FROM tenant_subscriptions ts
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        LEFT JOIN tenant_quota_overrides tq
          ON tq.tenant_id = ts.tenant_id
         AND tq.resource = $2
        WHERE ts.tenant_id = $1
          AND ts.status IN ('active', 'past_due')
          AND ts.current_period_end > now()
        ORDER BY ts.current_period_end DESC
        LIMIT 1
        """,
        tenant_id,
        resource,
    )
    if plan:
        return plan

    effective_slug = await get_effective_plan_slug(conn, tenant_id)
    if effective_slug != STARTER_PLAN_SLUG:
        return None

    return await conn.fetchrow(
        """
        SELECT
            sp.slug AS plan_slug,
            sp.features AS plan_features,
            date_trunc('month', now()) AS current_period_start,
            date_trunc('month', now()) + interval '1 month' AS current_period_end,
            tq.id AS override_id,
            tq.limit_override,
            COALESCE(tq.disabled, false) AS override_disabled,
            tq.reason AS override_reason
        FROM subscription_plans sp
        LEFT JOIN tenant_quota_overrides tq
          ON tq.tenant_id = $1
         AND tq.resource = $2
        WHERE sp.slug = $3
          AND sp.is_active = true
        LIMIT 1
        """,
        tenant_id,
        resource,
        STARTER_PLAN_SLUG,
    )


async def _count_period_quota_usage(
    conn,
    tenant_id: UUID,
    resource: str,
    period_start: datetime,
    period_end: datetime,
) -> int:
    if resource == "direct_purchases_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_purchases
            WHERE tenant_id = $1
              AND is_direct_entry = TRUE
              AND created_at >= $2
              AND created_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "stock_adjustments_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_ingredient_movements
            WHERE tenant_id = $1
              AND movement_type = 'adjustment'
              AND COALESCE(reference_table, '') <> 'tenant_purchases'
              AND created_at >= $2
              AND created_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "cash_closes_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM accounting_period
            WHERE tenant_id = $1
              AND closed_at >= $2
              AND closed_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "expenses_per_period":
        value = await conn.fetchval(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM tenant_expenses
                    WHERE tenant_id = $1
                      AND created_at >= $2
                      AND created_at < $3
                )
                +
                (
                    SELECT COUNT(*)
                    FROM recurring_expense_instances
                    WHERE tenant_id = $1
                      AND created_at >= $2
                      AND created_at < $3
                )
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "supplier_payments_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_purchases
            WHERE tenant_id = $1
              AND paid_at IS NOT NULL
              AND paid_at >= $2
              AND paid_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "expense_payments_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_expenses
            WHERE tenant_id = $1
              AND paid_at IS NOT NULL
              AND lower(COALESCE(payment_type, '')) = 'credito'
              AND paid_at >= $2
              AND paid_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "accounting_period_closes_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_monthly_periods
            WHERE tenant_id = $1
              AND status = 'closed'
              AND closed_at >= $2
              AND closed_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    elif resource == "manual_journal_entries_per_period":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_journal_entries
            WHERE tenant_id = $1
              AND source_module IN ('manual', 'manual_balance_adjustment')
              AND created_at >= $2
              AND created_at < $3
            """,
            tenant_id,
            period_start,
            period_end,
        )
    else:
        raise ValueError(f"Unsupported period quota resource: {resource}")

    return int(value or 0)


async def _get_period_quota_state(
    conn,
    tenant_id: UUID,
    resource: str,
) -> Optional[PeriodQuotaState]:
    if resource not in PERIOD_QUOTA_RESOURCES:
        raise ValueError(f"Unsupported period quota resource: {resource}")

    plan = await _fetch_period_quota_context(conn, tenant_id, resource)
    if not plan:
        return None

    quotas = _normalize_plan_quotas(plan["plan_slug"], plan["plan_features"])
    quota = _effective_quota_from_row(resource, quotas.get(resource, 0), plan)
    if quota.limit is None:
        return None

    period_start = plan["current_period_start"]
    period_end = plan["current_period_end"]
    used = await _count_period_quota_usage(
        conn,
        tenant_id,
        resource,
        period_start,
        period_end,
    )
    return PeriodQuotaState(
        plan_slug=plan["plan_slug"],
        quota=quota,
        period_start=period_start,
        period_end=period_end,
        used=used,
    )


async def check_plan_quota_period(conn, tenant_id: UUID, resource: str) -> None:
    """
    Block create once the tenant reaches a period-based operational quota.

    Usage window = current subscription period (calendar month for starter
    without a subscription row). Same 429 quota_exceeded payload as growth.
    """
    state = await _get_period_quota_state(conn, tenant_id, resource)
    if state is None:
        return

    if state.used < state.quota.limit:
        return

    _log_quota_block(
        tenant_id=tenant_id,
        resource=resource,
        used=state.used,
        quota=state.quota,
        plan_slug=state.plan_slug,
    )
    raise APIError(
        "Límite del plan alcanzado",
        status_code=429,
        details={
            "code": "quota_exceeded",
            "error": "quota_exceeded",
            "resource": resource,
            "used": state.used,
            "limit": state.quota.limit,
            "plan_limit": state.quota.plan_limit,
            "plan_slug": state.plan_slug,
            "override": _quota_override_payload(state.quota),
            "period_start": state.period_start.isoformat(),
            "period_end": state.period_end.isoformat(),
            "upgrade_url": QUOTA_UPGRADE_URL,
            "message": QUOTA_CONTACT_MESSAGE,
        },
    )


async def _get_completed_online_order_quota_state(conn, tenant_id: UUID) -> Optional[OnlineOrderQuotaState]:
    """
    Read current online-order quota state.

    Returns None when no finite active quota applies, matching checkout's
    fail-open behavior for tenants without an active paid period.
    """
    plan = await conn.fetchrow(
        """
        SELECT
            sp.slug AS plan_slug,
            sp.features AS plan_features,
            ts.current_period_start,
            ts.current_period_end,
            tq.id AS override_id,
            tq.limit_override,
            COALESCE(tq.disabled, false) AS override_disabled,
            tq.reason AS override_reason
        FROM tenant_subscriptions ts
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        LEFT JOIN tenant_quota_overrides tq
          ON tq.tenant_id = ts.tenant_id
         AND tq.resource = $2
        WHERE ts.tenant_id = $1
          AND ts.status IN ('active', 'past_due')
          AND ts.current_period_end > now()
        ORDER BY ts.current_period_end DESC
        LIMIT 1
        """,
        tenant_id,
        ONLINE_ORDER_QUOTA_RESOURCE,
    )
    if not plan:
        effective_slug = await get_effective_plan_slug(conn, tenant_id)
        if effective_slug != STARTER_PLAN_SLUG:
            return None
        plan = await conn.fetchrow(
            """
            SELECT
                sp.slug AS plan_slug,
                sp.features AS plan_features,
                date_trunc('month', now()) AS current_period_start,
                date_trunc('month', now()) + interval '1 month' AS current_period_end,
                tq.id AS override_id,
                tq.limit_override,
                COALESCE(tq.disabled, false) AS override_disabled,
                tq.reason AS override_reason
            FROM subscription_plans sp
            LEFT JOIN tenant_quota_overrides tq
              ON tq.tenant_id = $1
             AND tq.resource = $2
            WHERE sp.slug = $3
              AND sp.is_active = true
            LIMIT 1
            """,
            tenant_id,
            ONLINE_ORDER_QUOTA_RESOURCE,
            STARTER_PLAN_SLUG,
        )
        if not plan:
            return None

    quotas = _normalize_plan_quotas(plan["plan_slug"], plan["plan_features"])
    quota = _effective_quota_from_row(
        ONLINE_ORDER_QUOTA_RESOURCE,
        quotas.get(ONLINE_ORDER_QUOTA_RESOURCE, 0),
        plan,
    )
    if quota.limit is None or quota.limit <= 0:
        return None

    period_start = plan["current_period_start"]
    period_end = plan["current_period_end"]
    used = int(await conn.fetchval(
        """
        SELECT COUNT(DISTINCT o.id)
        FROM orders o
        WHERE o.tenant_id = $1
          AND o.online_cart_id IS NOT NULL
          AND o.status = 'completed'
          AND o.order_date >= $2
          AND o.order_date < $3
        """,
        tenant_id,
        period_start,
        period_end,
    ) or 0)

    return OnlineOrderQuotaState(
        plan_slug=plan["plan_slug"],
        quota=quota,
        period_start=period_start,
        period_end=period_end,
        used=used,
    )


async def get_public_online_order_quota_availability(conn, tenant_id: UUID) -> Dict[str, Any]:
    """
    Public-safe availability for online-order quota.

    This intentionally returns only a boolean, reason, and customer copy. It
    does not expose usage, plan, period, or override metadata.
    """
    state = await _get_completed_online_order_quota_state(conn, tenant_id)
    if state is None or state.used < state.quota.limit:
        return {
            "available": True,
            "reason": None,
            "message": None,
        }

    return {
        "available": False,
        "reason": "online_order_quota_exceeded",
        "message": ONLINE_ORDER_QUOTA_CUSTOMER_MESSAGE,
    }


async def check_completed_online_order_quota(conn, tenant_id: UUID) -> None:
    """
    Block final public checkout once the tenant reaches its online-order quota.

    Unlike active-resource quotas, this is period-based: the usage window comes
    from the current subscription period and counts only completed online orders.
    """
    state = await _get_completed_online_order_quota_state(conn, tenant_id)
    if state is None:
        return

    _log_online_order_quota_usage(
        tenant_id=tenant_id,
        plan_slug=state.plan_slug,
        used=state.used,
        limit=state.quota.limit,
        period_start=state.period_start,
        period_end=state.period_end,
    )

    if state.used < state.quota.limit:
        return

    _log_quota_block(
        tenant_id=tenant_id,
        resource=ONLINE_ORDER_QUOTA_RESOURCE,
        used=state.used,
        quota=state.quota,
        plan_slug=state.plan_slug,
    )
    raise APIError(
        "Límite mensual de pedidos en línea alcanzado",
        status_code=429,
        details={
            "code": "online_order_quota_exceeded",
            "error": "online_order_quota_exceeded",
            "resource": ONLINE_ORDER_QUOTA_RESOURCE,
            "used": state.used,
            "limit": state.quota.limit,
            "plan_limit": state.quota.plan_limit,
            "plan_slug": state.plan_slug,
            "override": _quota_override_payload(state.quota),
            "period_start": state.period_start.isoformat(),
            "period_end": state.period_end.isoformat(),
            "upgrade_url": QUOTA_UPGRADE_URL,
            "tenant_message": QUOTA_CONTACT_MESSAGE,
            "customer_message": ONLINE_ORDER_QUOTA_CUSTOMER_MESSAGE,
        },
    )


def _log_online_order_quota_usage(
    *,
    tenant_id: UUID,
    plan_slug: str,
    used: int,
    limit: int,
    period_start: datetime,
    period_end: datetime,
) -> None:
    usage_ratio = used / limit if limit else 0
    if usage_ratio >= 1:
        logger.warning(
            "online_order_quota_threshold tenant=%s plan=%s used=%d limit=%d threshold=100 period_start=%s period_end=%s",
            tenant_id,
            plan_slug,
            used,
            limit,
            period_start.isoformat(),
            period_end.isoformat(),
        )
    elif usage_ratio >= 0.8:
        logger.info(
            "online_order_quota_threshold tenant=%s plan=%s used=%d limit=%d threshold=80 period_start=%s period_end=%s",
            tenant_id,
            plan_slug,
            used,
            limit,
            period_start.isoformat(),
            period_end.isoformat(),
        )


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _effective_quota_from_row(resource: str, plan_limit: Any, row: Any) -> EffectiveQuota:
    plan_limit_int = _coerce_quota_value(plan_limit, 0)
    override_id = _row_value(row, "override_id")
    if not override_id:
        return EffectiveQuota(resource=resource, plan_limit=plan_limit_int, limit=plan_limit_int)

    disabled = bool(_row_value(row, "override_disabled", False))
    limit_override = _row_value(row, "limit_override")
    effective_limit = None if disabled else _coerce_quota_value(limit_override, plan_limit_int)
    return EffectiveQuota(
        resource=resource,
        plan_limit=plan_limit_int,
        limit=effective_limit,
        override_id=override_id,
        override_disabled=disabled,
        override_reason=_row_value(row, "override_reason"),
    )


def _quota_override_payload(quota: EffectiveQuota) -> Optional[Dict[str, Any]]:
    if not quota.has_override:
        return None
    return {
        "id": str(quota.override_id),
        "disabled": quota.override_disabled,
        "reason": quota.override_reason,
    }


def _log_quota_block(
    *,
    tenant_id: UUID,
    resource: str,
    used: int,
    quota: EffectiveQuota,
    plan_slug: str,
) -> None:
    logger.warning(
        "quota_blocked tenant=%s resource=%s used=%d limit=%s plan_limit=%d plan=%s override=%s override_id=%s",
        tenant_id,
        resource,
        used,
        quota.limit,
        quota.plan_limit,
        plan_slug,
        quota.has_override,
        quota.override_id,
    )


async def _fetch_tenant_quota_overrides(conn, tenant_id: UUID) -> Dict[str, EffectiveQuota]:
    rows = await conn.fetch(
        """
        SELECT id, resource, limit_override, disabled, reason
        FROM tenant_quota_overrides
        WHERE tenant_id = $1
          AND resource = ANY($2::text[])
        """,
        tenant_id,
        list(QUOTA_KEYS),
    )
    return {
        row["resource"]: EffectiveQuota(
            resource=row["resource"],
            plan_limit=0,
            limit=None if row["disabled"] else _coerce_quota_value(row["limit_override"], 0),
            override_id=row["id"],
            override_disabled=bool(row["disabled"]),
            override_reason=row["reason"],
        )
        for row in rows
    }


def _apply_effective_quotas(
    plan_quotas: Dict[str, int],
    overrides: Dict[str, EffectiveQuota],
) -> Dict[str, EffectiveQuota]:
    effective: Dict[str, EffectiveQuota] = {}
    for resource in QUOTA_KEYS:
        plan_limit = _coerce_quota_value(plan_quotas.get(resource), 0)
        override = overrides.get(resource)
        if override:
            effective[resource] = EffectiveQuota(
                resource=resource,
                plan_limit=plan_limit,
                limit=None if override.override_disabled else override.limit,
                override_id=override.override_id,
                override_disabled=override.override_disabled,
                override_reason=override.override_reason,
            )
        else:
            effective[resource] = EffectiveQuota(
                resource=resource,
                plan_limit=plan_limit,
                limit=plan_limit,
            )
    return effective


async def _count_quota_resource_usage(
    conn,
    tenant_id: UUID,
    resource: str,
    *,
    exclude_pending_invitation_id: Optional[UUID] = None,
) -> int:
    if resource == "admin_users":
        from app.core.platform_superusers import platform_superuser_email_list

        allowlist = platform_superuser_email_list()
        value = await conn.fetchval(
            """
            SELECT
                (
                    SELECT COUNT(DISTINCT tm.id)
                    FROM tenant_members tm
                    INNER JOIN profile p ON p.id = tm.user_id
                    WHERE tm.tenant_id = $1
                      AND tm.is_active
                      AND tm.role = ANY($2::text[])
                      AND (
                        cardinality($4::text[]) = 0
                        OR lower(trim(p.email)) <> ALL($4::text[])
                      )
                )
                +
                (
                    SELECT COUNT(DISTINCT ti.id)
                    FROM tenant_invitations ti
                    WHERE ti.tenant_id = $1
                      AND ti.status = 'pending'
                      AND ti.role = ANY($2::text[])
                      AND NOT ($3::uuid IS NOT NULL AND ti.id = $3)
                      AND (
                        cardinality($4::text[]) = 0
                        OR lower(trim(ti.email)) <> ALL($4::text[])
                      )
                )
            """,
            tenant_id,
            list(LEGACY_INTERNAL_TEAM_ROLES),
            exclude_pending_invitation_id,
            allowlist,
        )
    elif resource == "active_kitchens":
        value = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT ks.id)
            FROM kitchen_stations ks
            WHERE ks.tenant_id = $1
              AND ks.is_active
            """,
            tenant_id,
        )
    elif resource == "active_tables_including_bar":
        value = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT t.id)
            FROM tables t
            WHERE t.tenant_id = $1
              AND t.is_active
              AND t.deleted_at IS NULL
            """,
            tenant_id,
        )
    elif resource == "active_qr_tables":
        value = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT t.id)
            FROM tables t
            WHERE t.tenant_id = $1
              AND t.is_active
              AND t.deleted_at IS NULL
              AND t.qr_enabled
            """,
            tenant_id,
        )
    elif resource == "menu_products":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM product
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    elif resource == "menu_categories":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM categories
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    elif resource == "tenant_ingredients":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM ingredients
            WHERE tenant_id = $1
              AND is_active = TRUE
            """,
            tenant_id,
        )
    elif resource == "tenant_suppliers":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_suppliers
            WHERE tenant_id = $1
              AND is_active = TRUE
            """,
            tenant_id,
        )
    elif resource == "active_open_cash_shifts":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM cash_shift_openings
            WHERE tenant_id = $1
              AND status = 'open'
            """,
            tenant_id,
        )
    elif resource == "payment_methods":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM payment_methods
            WHERE tenant_id = $1
              AND is_active = TRUE
            """,
            tenant_id,
        )
    elif resource == "api_tokens":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM api_tokens
            WHERE tenant_id = $1
              AND is_active = TRUE
            """,
            tenant_id,
        )
    elif resource == "tenant_promotions":
        # Count all promotion rows (hard delete frees a slot; is_active toggle does not).
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tenant_promotions
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    elif resource == "modifier_groups":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM modifier_groups
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    elif resource == "recipe_bases":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM product_base_types
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    else:
        raise ValueError(f"Unsupported quota resource: {resource}")

    return int(value or 0)


async def _count_scoped_quota_usage(conn, resource: str, scope_id: UUID) -> int:
    if resource == "recipe_lines_per_product":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM product_recipes
            WHERE product_id = $1
            """,
            scope_id,
        )
    elif resource == "modifier_options_per_group":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM modifiers
            WHERE modifier_group_id = $1
              AND is_available = TRUE
              AND removed_at IS NULL
            """,
            scope_id,
        )
    elif resource == "recipe_base_template_lines":
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM base_recipe_templates
            WHERE product_base_type_id = $1
            """,
            scope_id,
        )
    else:
        raise ValueError(f"Unsupported scoped quota resource: {resource}")

    return int(value or 0)


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
    scan_limit: int = sub["scan_limit"] if sub else await _default_scan_limit_for_tenant(conn, tenant_id)

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
        scans_limit = await _default_scan_limit_for_tenant(conn, tenant_id)
        return {
            "scans_used": 0,
            "scans_limit": scans_limit,
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
    visible_types = list(CUSTOMER_VISIBLE_BILLING_EVENT_TYPES)
    rows = await conn.fetch("""
        SELECT
            be.id, be.tenant_id, t.name AS tenant_name,
            be.subscription_id, be.event_type, be.amount,
            be.currency, be.metadata, be.created_at
        FROM billing_events be
        JOIN tenants t ON t.id = be.tenant_id
        WHERE be.tenant_id = $3
          AND be.event_type = ANY($4::text[])
        ORDER BY be.created_at DESC
        LIMIT $1 OFFSET $2
    """, limit, offset, tenant_id, visible_types)
    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM billing_events
        WHERE tenant_id = $1
          AND event_type = ANY($2::text[])
        """,
        tenant_id,
        visible_types,
    )
    return _serialize_billing_events(rows, total, limit, offset)


# ── Serialization helpers ─────────────────────────────────────────────────────

def _serialize_plan(row) -> Dict[str, Any]:
    features = _jsonb_to_dict(row["features"])
    quotas = _normalize_plan_quotas(row["slug"], features)
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "price_monthly": str(row["price_monthly"]),
        "price_annual": str(row["price_annual"]),
        "scan_limit": row["scan_limit"],
        "is_active": row["is_active"],
        "features": features,
        "quotas": quotas,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _jsonb_to_dict(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _coerce_quota_value(value: Any, default: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def _normalize_plan_quotas(plan_slug: str, features: Any) -> Dict[str, int]:
    plan_features = _jsonb_to_dict(features)
    defaults = PLAN_QUOTA_DEFAULTS.get(plan_slug, {})
    configured = plan_features.get(QUOTAS_FEATURE)
    if not isinstance(configured, dict):
        configured = {}

    quotas: Dict[str, int] = {}
    for key in QUOTA_KEYS:
        default = defaults.get(key, 0)
        quotas[key] = _coerce_quota_value(configured.get(key), default)

    if plan_slug == ELECTRONIC_INVOICE_PLAN_SLUG and not configured.get("electronic_invoices_per_period"):
        quotas["electronic_invoices_per_period"] = _electronic_invoice_limit_for_plan(
            plan_slug,
            plan_features,
        )

    return quotas


def _electronic_invoice_limit_for_plan(plan_slug: str, features: Any) -> int:
    if plan_slug != ELECTRONIC_INVOICE_PLAN_SLUG:
        return 0

    plan_features = _jsonb_to_dict(features)
    configured_quotas = plan_features.get(QUOTAS_FEATURE)
    if isinstance(configured_quotas, dict) and "electronic_invoices_per_period" in configured_quotas:
        return _coerce_quota_value(configured_quotas.get("electronic_invoices_per_period"), 0)

    configured_limit = plan_features.get(ELECTRONIC_INVOICE_LIMIT_FEATURE)
    if configured_limit is None:
        return ELECTRONIC_INVOICE_PERIOD_LIMIT

    try:
        return max(int(configured_limit), 0)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s feature for plan=%s: %r",
            ELECTRONIC_INVOICE_LIMIT_FEATURE,
            plan_slug,
            configured_limit,
        )
        return 0


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
        SELECT id, name, slug, description, price_monthly, price_annual,
               scan_limit, features
        FROM subscription_plans
        WHERE id = $1 AND is_active = true
    """, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado o inactivo")
    amount_in_cents = annual_price_in_cents(row["price_annual"])
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "price_monthly": row["price_monthly"],
        "price_annual": row["price_annual"],
        "amount_in_cents": amount_in_cents,
        "scan_limit": row["scan_limit"],
        "features": _jsonb_to_dict(row["features"]),
    }


def annual_price_in_cents(value: Any) -> int:
    """Convert a server-owned COP annual price to exact minor units."""
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="El plan no tiene precio anual válido") from exc
    if not price.is_finite() or price <= 0:
        raise HTTPException(status_code=422, detail="El plan no tiene precio anual cobrable")
    cents = price * Decimal("100")
    if cents != cents.to_integral_value():
        raise HTTPException(
            status_code=422,
            detail="El precio anual contiene una fracción menor a un centavo",
        )
    return int(cents)


async def list_onboarding_plans(conn) -> List[Dict[str, Any]]:
    """Return every active annual COP plan from the server-owned catalog."""
    rows = await conn.fetch("""
        SELECT id, name, slug, description, price_annual, features
        FROM subscription_plans
        WHERE is_active = true
        ORDER BY price_annual ASC, name ASC
    """)
    plans: List[Dict[str, Any]] = []
    for row in rows:
        annual_price_in_cents(row["price_annual"])
        plans.append({
            "id": row["id"],
            "name": row["name"],
            "slug": row["slug"],
            "description": row["description"],
            "priceAnnual": row["price_annual"],
            "currency": "COP",
            "billingCycle": "annual",
            "features": _jsonb_to_dict(row["features"]),
        })
    return plans


async def create_onboarding_payment_attempt(
    conn,
    *,
    tenant_id: UUID,
    plan_id: UUID,
    amount_in_cents: int,
    provider_environment: str = "prod",
    currency: str = "COP",
    provider: str = "paddle",
) -> UUID:
    """Create immutable attempt evidence before requesting a checkout link."""
    if provider_environment not in ("prod", "test"):
        raise HTTPException(status_code=422, detail="Invalid payment provider environment")
    currency_norm = str(currency or "COP").strip().upper()
    provider_norm = str(provider or "paddle").strip().lower()
    if provider_norm == "wompi":
        raise HTTPException(
            status_code=422,
            detail="Wompi is deprecated for new billing payments; use Paddle",
        )
    if provider_norm not in ("paddle",):
        raise HTTPException(status_code=422, detail="Invalid payment provider")
    if currency_norm not in ("COP", "USD", "EUR"):
        raise HTTPException(status_code=422, detail="Invalid payment currency")
    row = await conn.fetchrow("""
        INSERT INTO billing_payment_attempts (
            tenant_id, plan_id, provider, expected_amount_in_cents,
            currency, billing_cycle, status, provider_environment
        )
        VALUES ($1, $2, $3, $4, $5, 'annual', 'created', $6)
        RETURNING id
    """, tenant_id, plan_id, provider_norm, amount_in_cents, currency_norm, provider_environment)
    return row["id"]


async def attach_onboarding_payment_link(
    conn,
    *,
    attempt_id: UUID,
    tenant_id: UUID,
    provider_reference: str,
    checkout_url: str,
) -> None:
    """Attach the provider link once without replacing earlier attempt evidence."""
    row = await conn.fetchrow("""
        UPDATE billing_payment_attempts
        SET provider_reference = $3,
            checkout_url = $4,
            status = 'pending',
            updated_at = now()
        WHERE id = $1
          AND tenant_id = $2
          AND status = 'created'
          AND provider_reference IS NULL
        RETURNING id
    """, attempt_id, tenant_id, provider_reference, checkout_url)
    if row is None:
        raise HTTPException(status_code=409, detail="El intento de pago ya no está disponible")


async def get_onboarding_payment_attempt(
    conn,
    *,
    tenant_id: UUID,
    attempt_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    if attempt_id is None:
        row = await conn.fetchrow(
            """
            SELECT id, plan_id, provider_reference, provider_transaction_id,
                   expected_amount_in_cents, currency, status
            FROM billing_payment_attempts
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tenant_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT id, plan_id, provider_reference, provider_transaction_id,
                   expected_amount_in_cents, currency, status
            FROM billing_payment_attempts
            WHERE id = $1 AND tenant_id = $2
            """,
            attempt_id,
            tenant_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Intento de pago no encontrado")
    return {
        "attempt_id": row["id"],
        "plan_id": row["plan_id"],
        "provider_reference": row["provider_reference"],
        "provider_transaction_id": row["provider_transaction_id"],
        "amount_in_cents": row["expected_amount_in_cents"],
        "currency": row["currency"].strip(),
        "status": row["status"],
    }


async def payment_reference_belongs_to_tenant(
    conn,
    *,
    tenant_id: UUID,
    provider_reference: str,
) -> bool:
    row = await conn.fetchval("""
        SELECT 1
        FROM billing_payment_attempts
        WHERE tenant_id = $1 AND provider_reference = $2
        UNION ALL
        SELECT 1
        FROM tenant_subscriptions
        WHERE tenant_id = $1 AND gateway_reference = $2
        LIMIT 1
    """, tenant_id, provider_reference)
    return row is not None


async def ensure_subscribe_allowed(conn, tenant_id: UUID) -> None:
    """Block mid-period rebill for active annuals still inside current_period_end (#797)."""
    from app.core.billing_pricing import is_grandfathered_annual

    row = await conn.fetchrow(
        """
        SELECT status, billing_cycle, current_period_end
        FROM tenant_subscriptions
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if row is None:
        return
    if is_grandfathered_annual(
        status=row["status"],
        billing_cycle=row["billing_cycle"],
        current_period_end=row["current_period_end"],
    ):
        period_end = row["current_period_end"]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "grandfather_active_period",
                "message": "Active annual subscription is already paid through the current period.",
                "current_period_end": period_end.isoformat() if period_end else None,
            },
        )


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
    checkout reference, setting status='pending' until the webhook confirms.

    Defense in depth: refuses to overwrite a grandfathered active annual (#797).
    """
    await ensure_subscribe_allowed(conn, tenant_id)

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
        WHERE NOT (
            tenant_subscriptions.status = 'active'
            AND tenant_subscriptions.billing_cycle = 'annual'
            AND tenant_subscriptions.current_period_end IS NOT NULL
            AND tenant_subscriptions.current_period_end > now()
        )
        RETURNING id, tenant_id, plan_id, billing_cycle, status,
                  gateway_reference, current_period_start, current_period_end
    """, tenant_id, plan_id, billing_cycle, gateway_reference)

    if row is None:
        # Concurrent race: still grandfathered after ensure_subscribe_allowed.
        await ensure_subscribe_allowed(conn, tenant_id)
        raise HTTPException(status_code=409, detail="Unable to start subscription checkout")

    sub_id = row["id"]

    await conn.execute("""
        INSERT INTO billing_events
            (tenant_id, subscription_id, event_type, metadata)
        VALUES ($1, $2, 'subscribe_initiated', $3)
    """, tenant_id, sub_id, json.dumps({
        "checkout_url": checkout_url,
        "plan_id": str(plan_id),
        "provider": "paddle",
    }))

    logger.info(
        "subscribe_initiated: tenant=%s plan=%s cycle=%s gateway_ref=%s",
        tenant_id, plan_id, billing_cycle, gateway_reference,
    )

    return {
        "subscription_id": str(sub_id),
        "checkout_url": checkout_url,
        "gateway_reference": gateway_reference,
        "status": "pending",
    }


_ACTIVATABLE_STATUSES = frozenset({"pending", "past_due", "expired"})
_RENEWABLE_STATUSES = frozenset({"pending", "past_due", "expired", "active"})

# Tenant-facing Mi Plan history (GET /billing/events) — excludes cron/ops noise.
CUSTOMER_VISIBLE_BILLING_EVENT_TYPES = (
    "subscribe_initiated",
    "payment_approved",
    "payment_rejected",
    "payment_failed",
    "payment_pending",
    "subscription_cancelled",
    "subscription_created",
    "subscription_renewed",
    "subscription_expired",
    "gift_granted",
    "plan_changed",
)


def parse_wompi_period_anchor(transaction: Dict[str, Any]) -> datetime:
    """Anchor billing period to Wompi payment time, not webhook processing time."""
    for key in ("finalized_at", "created_at"):
        raw = transaction.get(key)
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc)


async def payment_approved_exists(
    conn,
    subscription_id: UUID,
    wompi_transaction_id: str = "",
    *,
    paddle_transaction_id: Optional[str] = None,
) -> bool:
    """True if this provider transaction already recorded as payment_approved."""
    if paddle_transaction_id:
        row = await conn.fetchval(
            """
            SELECT 1 FROM billing_events
            WHERE subscription_id = $1
              AND event_type = 'payment_approved'
              AND metadata->>'paddle_transaction_id' = $2
            LIMIT 1
            """,
            subscription_id,
            paddle_transaction_id,
        )
        return row is not None
    if not wompi_transaction_id:
        return False
    row = await conn.fetchval(
        """
        SELECT 1 FROM billing_events
        WHERE subscription_id = $1
          AND event_type = 'payment_approved'
          AND metadata->>'wompi_transaction_id' = $2
        LIMIT 1
        """,
        subscription_id,
        wompi_transaction_id,
    )
    return row is not None


async def _activate_subscription_with_period(
    conn,
    *,
    subscription_id: UUID,
    tenant_id: UUID,
    billing_cycle: str,
    amount: float,
    currency: str,
    metadata: Dict[str, Any],
    period_anchor: Optional[datetime] = None,
) -> Optional[datetime]:
    """Set subscription active, extend billing period, record payment_approved."""
    # Interval literals stay in SQL — asyncpg cannot bind '1 year' strings as interval.
    cycle = billing_cycle if billing_cycle in ("monthly", "annual") else "annual"
    anchor = period_anchor or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    updated = await conn.fetchrow("""
        UPDATE tenant_subscriptions
        SET status               = 'active',
            current_period_start = $3::timestamptz,
            current_period_end   = $3::timestamptz + CASE
                WHEN $2::text = 'monthly' THEN interval '1 month'
                ELSE interval '1 year'
            END,
            updated_at           = now()
        WHERE id = $1
        RETURNING current_period_end
    """, subscription_id, cycle, anchor)

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
    wompi_transaction_id: str = "",
    amount: float = 0.0,
    period_anchor: Optional[datetime] = None,
    *,
    currency: str = "COP",
    paddle_transaction_id: Optional[str] = None,
    paddle_subscription_id: Optional[str] = None,
    provider: str = "paddle",
    provider_environment: Optional[str] = None,
) -> bool:
    """
    Activate or renew tenant subscription when payment provider confirms payment.

    Lookup order (#797):
      1) tenant_id + gateway_reference (pending checkout / past_due)
      2) tenant_id only for Paddle renew when gateway_ref rotated to a new txn

    Skips grandfathered active annuals (mid-period). Returns True only when a
    payment_approved event was written.
    """
    from app.core.billing_pricing import is_grandfathered_annual

    row = await conn.fetchrow(
        """SELECT id, status, billing_cycle, current_period_end, gateway_reference
           FROM tenant_subscriptions
           WHERE tenant_id = $1 AND gateway_reference = $2""",
        tenant_id, gateway_reference,
    )
    matched_gateway = row is not None

    if row is None and provider == "paddle":
        row = await conn.fetchrow(
            """SELECT id, status, billing_cycle, current_period_end, gateway_reference
               FROM tenant_subscriptions
               WHERE tenant_id = $1""",
            tenant_id,
        )

    if not row:
        logger.warning(
            "activate_subscription: no subscription found for tenant=%s gateway_ref=%s",
            tenant_id, gateway_reference,
        )
        return False

    if is_grandfathered_annual(
        status=row["status"],
        billing_cycle=row["billing_cycle"],
        current_period_end=row.get("current_period_end"),
    ):
        logger.info(
            "activate_subscription: grandfathered active annual tenant=%s — skipped",
            tenant_id,
        )
        return False

    if row["status"] not in _RENEWABLE_STATUSES:
        logger.warning(
            "activate_subscription: status=%s not renewable tenant=%s gateway_ref=%s",
            row["status"], tenant_id, gateway_reference,
        )
        return False

    # Same gateway_ref on already-active row: duplicate webhook / no-op.
    if row["status"] == "active" and matched_gateway:
        logger.info("activate_subscription: already active tenant=%s", tenant_id)
        return False

    if await payment_approved_exists(
        conn,
        row["id"],
        wompi_transaction_id,
        paddle_transaction_id=paddle_transaction_id,
    ):
        logger.info(
            "activate_subscription: duplicate provider txn tenant=%s — skipped",
            tenant_id,
        )
        return False

    metadata: Dict[str, Any] = {
        "gateway_reference": gateway_reference,
        "provider": provider,
    }
    if wompi_transaction_id:
        metadata["wompi_transaction_id"] = wompi_transaction_id
    if paddle_transaction_id:
        metadata["paddle_transaction_id"] = paddle_transaction_id
    if paddle_subscription_id:
        metadata["paddle_subscription_id"] = paddle_subscription_id
    if provider_environment:
        metadata["provider_environment"] = provider_environment
    if not matched_gateway:
        metadata["renewal"] = True
        metadata["previous_gateway_reference"] = row.get("gateway_reference")
        if row.get("gateway_reference") != gateway_reference:
            await conn.execute(
                """
                UPDATE tenant_subscriptions
                SET gateway_reference = $2, updated_at = now()
                WHERE id = $1
                """,
                row["id"],
                gateway_reference,
            )

    period_end = await _activate_subscription_with_period(
        conn,
        subscription_id=row["id"],
        tenant_id=tenant_id,
        billing_cycle=row["billing_cycle"] or "annual",
        amount=amount,
        currency=currency,
        metadata=metadata,
        period_anchor=period_anchor,
    )
    await onboarding_service.activate_paid_onboarding_identity(conn, tenant_id)

    logger.info(
        "Subscription activated/renewed: tenant=%s provider=%s txn=%s amount=%s period_end=%s",
        tenant_id,
        provider,
        paddle_transaction_id or wompi_transaction_id,
        amount,
        period_end,
    )
    return True



async def get_tenant_billing_context(conn, tenant_id: UUID) -> Dict[str, Any]:
    """Country + slug for Paddle pricing/env resolution."""
    row = await conn.fetchrow(
        """
        SELECT t.slug,
               COALESCE(tfp.country_code, 'CO') AS country_code
        FROM tenants t
        LEFT JOIN tenant_financial_profiles tfp ON tfp.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if not row:
        return {"slug": None, "country_code": "CO"}
    return {"slug": row["slug"], "country_code": row["country_code"] or "CO"}


async def get_tenant_billing_context(conn, tenant_id: UUID) -> Dict[str, Any]:
    """Country + slug for Paddle pricing/env resolution (#794/#796)."""
    row = await conn.fetchrow(
        """
        SELECT t.slug,
               COALESCE(tfp.country_code, 'CO') AS country_code
        FROM tenants t
        LEFT JOIN tenant_financial_profiles tfp ON tfp.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if not row:
        return {"slug": None, "country_code": "CO"}
    return {"slug": row["slug"], "country_code": row["country_code"] or "CO"}


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
            sp.features AS plan_features,
            ts.billing_cycle,
            ts.status,
            ts.current_period_start,
            ts.current_period_end,
            ts.gateway_reference,
            ts.cancelled_at,
            ts.created_at,
            ts.updated_at,
            CASE
                WHEN ts.status = 'pending' THEN (
                    SELECT be.metadata->>'checkout_url'
                    FROM billing_events be
                    WHERE be.subscription_id = ts.id
                      AND be.event_type = 'subscribe_initiated'
                      AND NULLIF(be.metadata->>'checkout_url', '') IS NOT NULL
                    ORDER BY be.created_at DESC
                    LIMIT 1
                )
                ELSE NULL
            END AS checkout_url,
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
        "quotas": _normalize_plan_quotas(row["plan_slug"], row["plan_features"]),
        "billing_cycle": row["billing_cycle"],
        "status": row["status"],
        "current_period_start": row["current_period_start"].isoformat(),
        "current_period_end": row["current_period_end"].isoformat(),
        "gateway_reference": row["gateway_reference"],
        "cancelled_at": row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "checkout_url": row["checkout_url"],
        "scans_used": row["scans_used"],
    }
    return data


async def get_remaining_billing_usage(conn, tenant_id: UUID) -> Dict[str, Any]:
    """
    Return current-period remaining usage for scans, electronic invoices, and
    operational/catalog quotas.

    Paid tenants use the subscription period. Starter tenants without a
    subscription row use the calendar month and the active `starter` plan
    catalog (warocol.com#1796). Electronic invoice quota is only exposed for
    the paid FE plan; other plans intentionally report 0/0/0.
    """
    row = await conn.fetchrow("""
        SELECT
            ts.current_period_start,
            ts.current_period_end,
            sp.slug AS plan_slug,
            sp.features AS plan_features,
            sp.scan_limit AS plan_scan_limit,
            COALESCE(su.scans_used, 0) AS scans_used,
            COALESCE(su.scans_limit, sp.scan_limit) AS scans_limit
        FROM tenant_subscriptions ts
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        LEFT JOIN scan_usage su
            ON su.tenant_id = ts.tenant_id
           AND su.period_start <= now()
           AND su.period_end > now()
        WHERE ts.tenant_id = $1
    """, tenant_id)
    if row is None:
        effective_slug = await get_effective_plan_slug(conn, tenant_id)
        if effective_slug != STARTER_PLAN_SLUG:
            raise HTTPException(status_code=404, detail="Subscription not found")
        row = await conn.fetchrow(
            """
            SELECT
                date_trunc('month', now()) AS current_period_start,
                date_trunc('month', now()) + interval '1 month' AS current_period_end,
                sp.slug AS plan_slug,
                sp.features AS plan_features,
                sp.scan_limit AS plan_scan_limit,
                COALESCE(su.scans_used, 0) AS scans_used,
                COALESCE(su.scans_limit, sp.scan_limit) AS scans_limit
            FROM subscription_plans sp
            LEFT JOIN scan_usage su
                ON su.tenant_id = $1
               AND su.period_start <= now()
               AND su.period_end > now()
            WHERE sp.slug = $2
              AND sp.is_active = true
            LIMIT 1
            """,
            tenant_id,
            STARTER_PLAN_SLUG,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Subscription not found")

    period_start = row["current_period_start"]
    period_end = row["current_period_end"]
    period_start_iso = period_start.isoformat()
    period_end_iso = period_end.isoformat()

    scans_used = int(row["scans_used"] or 0)
    scans_limit = int(row["scans_limit"] or row["plan_scan_limit"] or 0)

    invoice_limit = _electronic_invoice_limit_for_plan(
        row["plan_slug"],
        row["plan_features"],
    )
    invoice_used = 0
    if invoice_limit > 0:
        invoice_used = int(await conn.fetchval("""
            SELECT COUNT(*)
            FROM electronic_invoices
            WHERE tenant_id = $1
              AND status = 'accepted'
              AND document_type = 'invoice'
              AND COALESCE(emitted_at, created_at) >= $2
              AND COALESCE(emitted_at, created_at) < $3
        """, tenant_id, period_start, period_end) or 0)

    plan_quotas = _normalize_plan_quotas(row["plan_slug"], row["plan_features"])
    effective_quotas = _apply_effective_quotas(
        plan_quotas,
        await _fetch_tenant_quota_overrides(conn, tenant_id),
    )
    quota_counts = await conn.fetchrow("""
        SELECT
            (
                SELECT
                    (
                        SELECT COUNT(DISTINCT tm.id)
                        FROM tenant_members tm
                        WHERE tm.tenant_id = $1
                          AND tm.is_active
                          AND tm.role = ANY($4::text[])
                    )
                    +
                    (
                        SELECT COUNT(DISTINCT ti.id)
                        FROM tenant_invitations ti
                        WHERE ti.tenant_id = $1
                          AND ti.status = 'pending'
                          AND ti.role = ANY($4::text[])
                    )
            ) AS admin_users,
            (
                SELECT COALESCE(MAX(active_session_count), 0)
                FROM (
                    SELECT COUNT(DISTINCT s.id) AS active_session_count
                    FROM tenant_members tm
                    JOIN sessions s
                      ON s.tenant_id = tm.tenant_id
                     AND s.user_id = tm.user_id
                    WHERE tm.tenant_id = $1
                      AND tm.is_active
                      AND tm.role = ANY($4::text[])
                      AND s.is_active
                      AND s.expires_at > now()
                    GROUP BY tm.user_id
                ) per_admin
            ) AS active_sessions_per_admin_user,
            (
                SELECT COUNT(DISTINCT ks.id)
                FROM kitchen_stations ks
                WHERE ks.tenant_id = $1
                  AND ks.is_active
            ) AS active_kitchens,
            (
                SELECT COUNT(DISTINCT t.id)
                FROM tables t
                WHERE t.tenant_id = $1
                  AND t.is_active
                  AND t.deleted_at IS NULL
            ) AS active_tables_including_bar,
            (
                SELECT COUNT(DISTINCT t.id)
                FROM tables t
                WHERE t.tenant_id = $1
                  AND t.is_active
                  AND t.deleted_at IS NULL
                  AND t.qr_enabled
            ) AS active_qr_tables,
            (
                SELECT COUNT(DISTINCT o.id)
                FROM orders o
                WHERE o.tenant_id = $1
                  AND o.online_cart_id IS NOT NULL
                  AND o.status = 'completed'
                  AND o.order_date >= $2
                  AND o.order_date < $3
            ) AS completed_online_orders_per_month,
            (
                SELECT COUNT(*)
                FROM product p
                WHERE p.tenant_id = $1
            ) AS menu_products,
            (
                SELECT COUNT(*)
                FROM categories c
                WHERE c.tenant_id = $1
            ) AS menu_categories,
            (
                SELECT COUNT(*)
                FROM ingredients i
                WHERE i.tenant_id = $1
                  AND i.is_active = TRUE
            ) AS tenant_ingredients,
            (
                SELECT COUNT(*)
                FROM tenant_suppliers ts
                WHERE ts.tenant_id = $1
                  AND ts.is_active = TRUE
            ) AS tenant_suppliers,
            (
                SELECT COUNT(*)
                FROM tenant_purchases tp
                WHERE tp.tenant_id = $1
                  AND tp.is_direct_entry = TRUE
                  AND tp.created_at >= $2
                  AND tp.created_at < $3
            ) AS direct_purchases_per_period,
            (
                SELECT COUNT(*)
                FROM tenant_ingredient_movements tim
                WHERE tim.tenant_id = $1
                  AND tim.movement_type = 'adjustment'
                  AND COALESCE(tim.reference_table, '') <> 'tenant_purchases'
                  AND tim.created_at >= $2
                  AND tim.created_at < $3
            ) AS stock_adjustments_per_period,
            (
                SELECT COUNT(*)
                FROM accounting_period ap
                WHERE ap.tenant_id = $1
                  AND ap.closed_at >= $2
                  AND ap.closed_at < $3
            ) AS cash_closes_per_period,
            (
                SELECT COUNT(*)
                FROM cash_shift_openings cso
                WHERE cso.tenant_id = $1
                  AND cso.status = 'open'
            ) AS active_open_cash_shifts,
            (
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM tenant_expenses te
                        WHERE te.tenant_id = $1
                          AND te.created_at >= $2
                          AND te.created_at < $3
                    )
                    +
                    (
                        SELECT COUNT(*)
                        FROM recurring_expense_instances rei
                        WHERE rei.tenant_id = $1
                          AND rei.created_at >= $2
                          AND rei.created_at < $3
                    )
            ) AS expenses_per_period,
            (
                SELECT COUNT(*)
                FROM tenant_purchases tp_paid
                WHERE tp_paid.tenant_id = $1
                  AND tp_paid.paid_at IS NOT NULL
                  AND tp_paid.paid_at >= $2
                  AND tp_paid.paid_at < $3
            ) AS supplier_payments_per_period,
            (
                SELECT COUNT(*)
                FROM tenant_expenses te_paid
                WHERE te_paid.tenant_id = $1
                  AND te_paid.paid_at IS NOT NULL
                  AND lower(COALESCE(te_paid.payment_type, '')) = 'credito'
                  AND te_paid.paid_at >= $2
                  AND te_paid.paid_at < $3
            ) AS expense_payments_per_period,
            (
                SELECT COUNT(*)
                FROM payment_methods pm
                WHERE pm.tenant_id = $1
                  AND pm.is_active = TRUE
            ) AS payment_methods,
            (
                SELECT COUNT(*)
                FROM api_tokens atok
                WHERE atok.tenant_id = $1
                  AND atok.is_active = TRUE
            ) AS api_tokens,
            (
                SELECT COUNT(*)
                FROM tenant_promotions tpromo
                WHERE tpromo.tenant_id = $1
            ) AS tenant_promotions,
            (
                SELECT COUNT(*)
                FROM tenant_monthly_periods tmp
                WHERE tmp.tenant_id = $1
                  AND tmp.status = 'closed'
                  AND tmp.closed_at >= $2
                  AND tmp.closed_at < $3
            ) AS accounting_period_closes_per_period,
            (
                SELECT COUNT(*)
                FROM tenant_journal_entries tje
                WHERE tje.tenant_id = $1
                  AND tje.source_module IN ('manual', 'manual_balance_adjustment')
                  AND tje.created_at >= $2
                  AND tje.created_at < $3
            ) AS manual_journal_entries_per_period,
            (
                SELECT COUNT(*)
                FROM modifier_groups mg
                WHERE mg.tenant_id = $1
            ) AS modifier_groups,
            (
                SELECT COUNT(*)
                FROM product_base_types pbt
                WHERE pbt.tenant_id = $1
            ) AS recipe_bases
    """, tenant_id, period_start, period_end, list(LEGACY_INTERNAL_TEAM_ROLES))

    def metric(used: int, quota: EffectiveQuota) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "used": used,
            "limit": quota.limit,
            "remaining": None if quota.limit is None else max(quota.limit - used, 0),
            "period_start": period_start_iso,
            "period_end": period_end_iso,
        }
        if quota.has_override:
            data["plan_limit"] = quota.plan_limit
            data["override"] = _quota_override_payload(quota)
        return data

    return {
        "period_start": period_start_iso,
        "period_end": period_end_iso,
        "scan_usage": {
            "used": scans_used,
            "limit": scans_limit,
            "remaining": max(scans_limit - scans_used, 0),
            "period_start": period_start_iso,
            "period_end": period_end_iso,
        },
        "electronic_invoice_usage": {
            "used": invoice_used,
            "limit": invoice_limit,
            "remaining": max(invoice_limit - invoice_used, 0),
            "period_start": period_start_iso,
            "period_end": period_end_iso,
        },
        "quota_usage": {
            "admin_users": metric(
                int(quota_counts["admin_users"] or 0),
                effective_quotas["admin_users"],
            ),
            "active_sessions_per_admin_user": metric(
                int(quota_counts["active_sessions_per_admin_user"] or 0),
                effective_quotas["active_sessions_per_admin_user"],
            ),
            "active_kitchens": metric(
                int(quota_counts["active_kitchens"] or 0),
                effective_quotas["active_kitchens"],
            ),
            "active_tables_including_bar": metric(
                int(quota_counts["active_tables_including_bar"] or 0),
                effective_quotas["active_tables_including_bar"],
            ),
            "active_qr_tables": metric(
                int(quota_counts["active_qr_tables"] or 0),
                effective_quotas["active_qr_tables"],
            ),
            "completed_online_orders_per_month": metric(
                int(quota_counts["completed_online_orders_per_month"] or 0),
                effective_quotas["completed_online_orders_per_month"],
            ),
            "electronic_invoices_per_period": metric(
                invoice_used,
                effective_quotas["electronic_invoices_per_period"],
            ),
            "menu_products": metric(
                int(quota_counts["menu_products"] or 0),
                effective_quotas["menu_products"],
            ),
            "menu_categories": metric(
                int(quota_counts["menu_categories"] or 0),
                effective_quotas["menu_categories"],
            ),
            "tenant_ingredients": metric(
                int(quota_counts["tenant_ingredients"] or 0),
                effective_quotas["tenant_ingredients"],
            ),
            "tenant_suppliers": metric(
                int(quota_counts["tenant_suppliers"] or 0),
                effective_quotas["tenant_suppliers"],
            ),
            "direct_purchases_per_period": metric(
                int(quota_counts["direct_purchases_per_period"] or 0),
                effective_quotas["direct_purchases_per_period"],
            ),
            "stock_adjustments_per_period": metric(
                int(quota_counts["stock_adjustments_per_period"] or 0),
                effective_quotas["stock_adjustments_per_period"],
            ),
            "cash_closes_per_period": metric(
                int(quota_counts["cash_closes_per_period"] or 0),
                effective_quotas["cash_closes_per_period"],
            ),
            "active_open_cash_shifts": metric(
                int(quota_counts["active_open_cash_shifts"] or 0),
                effective_quotas["active_open_cash_shifts"],
            ),
            "expenses_per_period": metric(
                int(quota_counts["expenses_per_period"] or 0),
                effective_quotas["expenses_per_period"],
            ),
            "supplier_payments_per_period": metric(
                int(quota_counts["supplier_payments_per_period"] or 0),
                effective_quotas["supplier_payments_per_period"],
            ),
            "expense_payments_per_period": metric(
                int(quota_counts.get("expense_payments_per_period") or 0),
                effective_quotas["expense_payments_per_period"],
            ),
            "payment_methods": metric(
                int(quota_counts["payment_methods"] or 0),
                effective_quotas["payment_methods"],
            ),
            "api_tokens": metric(
                int(quota_counts.get("api_tokens") or 0),
                effective_quotas["api_tokens"],
            ),
            "tenant_promotions": metric(
                int(quota_counts.get("tenant_promotions") or 0),
                effective_quotas["tenant_promotions"],
            ),
            "accounting_period_closes_per_period": metric(
                int(quota_counts["accounting_period_closes_per_period"] or 0),
                effective_quotas["accounting_period_closes_per_period"],
            ),
            "manual_journal_entries_per_period": metric(
                int(quota_counts["manual_journal_entries_per_period"] or 0),
                effective_quotas["manual_journal_entries_per_period"],
            ),
            "modifier_groups": metric(
                int(quota_counts["modifier_groups"] or 0),
                effective_quotas["modifier_groups"],
            ),
            "recipe_bases": metric(
                int(quota_counts["recipe_bases"] or 0),
                effective_quotas["recipe_bases"],
            ),
            # Scoped caps: limit is plan-level; used is always 0 here (count is
            # per product/group). Front compares local editor counts to limit.
            "recipe_lines_per_product": metric(
                0,
                effective_quotas["recipe_lines_per_product"],
            ),
            "modifier_options_per_group": metric(
                0,
                effective_quotas["modifier_options_per_group"],
            ),
        },
    }


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


def _webhook_amount_in_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Wompi amount mismatch") from exc
    if not amount.is_finite() or amount != amount.to_integral_value() or amount <= 0:
        raise HTTPException(status_code=409, detail="Wompi amount mismatch")
    return int(amount)


async def process_onboarding_payment_transaction(
    conn,
    transaction: Dict[str, Any],
    provider_environment: str = "prod",
) -> Dict[str, Any]:
    """Resolve an onboarding attempt and activate access only for a valid webhook."""
    provider_reference = str(transaction.get("payment_link_id") or "").strip()
    if not provider_reference:
        return {"handled": False, "tenant_info": None}

    attempt = await conn.fetchrow(
        """
        SELECT a.id, a.tenant_id, a.plan_id, a.provider_reference,
               a.expected_amount_in_cents, a.currency, a.status,
               a.provider_transaction_id, a.provider_environment,
               p.name AS plan_name, p.price_annual, p.is_active AS plan_is_active
        FROM billing_payment_attempts a
        JOIN subscription_plans p ON p.id = a.plan_id
        WHERE a.provider = 'wompi' AND a.provider_reference = $1
        ORDER BY (a.provider_environment = $2) DESC
        LIMIT 1
        FOR UPDATE OF a
        """,
        provider_reference,
        provider_environment,
    )
    if attempt is None:
        return {"handled": False, "tenant_info": None}
    if attempt["provider_environment"] != provider_environment:
        raise HTTPException(status_code=409, detail="Wompi environment mismatch")

    wompi_status = str(transaction.get("status") or "").upper()
    transaction_id = str(transaction.get("id") or "").strip()
    if not transaction_id:
        raise HTTPException(status_code=409, detail="Wompi transaction ID mismatch")

    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", transaction_id)
    duplicate_attempt_id = await conn.fetchval(
        """
        SELECT id
        FROM billing_payment_attempts
        WHERE provider_transaction_id = $1
          AND provider_environment = $2
          AND id <> $3
        LIMIT 1
        """,
        transaction_id,
        provider_environment,
        attempt["id"],
    )
    if duplicate_attempt_id is not None:
        raise HTTPException(status_code=409, detail="Wompi transaction ID mismatch")

    if wompi_status == "PENDING":
        if attempt["status"] != "approved":
            await conn.execute(
                """
                UPDATE billing_payment_attempts
                SET status = 'pending', provider_transaction_id = $2, updated_at = now()
                WHERE id = $1
                """,
                attempt["id"],
                transaction_id,
            )
        return {"handled": True, "tenant_info": None}

    if wompi_status in ("DECLINED", "VOIDED", "ERROR"):
        if attempt["status"] != "approved":
            failed_status = "error" if wompi_status == "ERROR" else "declined"
            await conn.execute(
                """
                UPDATE billing_payment_attempts
                SET status = $2, provider_transaction_id = $3,
                    resolved_at = now(), updated_at = now()
                WHERE id = $1
                """,
                attempt["id"],
                failed_status,
                transaction_id,
            )
        return {"handled": True, "tenant_info": None}

    if wompi_status != "APPROVED":
        return {"handled": True, "tenant_info": None}

    amount_in_cents = _webhook_amount_in_cents(transaction.get("amount_in_cents"))
    currency = str(transaction.get("currency") or "").strip().upper()
    sku = str(transaction.get("sku") or "").strip()
    expected_currency = str(attempt["currency"]).strip().upper()
    expected_amount = int(attempt["expected_amount_in_cents"])
    catalog_amount = annual_price_in_cents(attempt["price_annual"])
    if (
        provider_reference != attempt["provider_reference"]
        or sku != str(attempt["id"])
        or currency != expected_currency
        or expected_currency != "COP"
        or amount_in_cents != expected_amount
        or catalog_amount != expected_amount
        or not attempt["plan_is_active"]
    ):
        raise HTTPException(status_code=409, detail="Wompi payment evidence mismatch")

    if attempt["status"] == "approved":
        if attempt["provider_transaction_id"] != transaction_id:
            raise HTTPException(status_code=409, detail="Wompi transaction ID mismatch")
        return {"handled": True, "tenant_info": None}

    existing_subscription = await conn.fetchrow(
        """
        SELECT id, status
        FROM tenant_subscriptions
        WHERE tenant_id = $1
        FOR UPDATE
        """,
        attempt["tenant_id"],
    )
    identity = await onboarding_service.activate_paid_onboarding_identity(
        conn, attempt["tenant_id"]
    )
    if identity is None:
        reconciliation_metadata = {
            "reason": "onboarding_already_activated",
            "payment_attempt_id": str(attempt["id"]),
            "wompi_transaction_id": transaction_id,
            "gateway_reference": provider_reference,
            "requested_plan_id": str(attempt["plan_id"]),
            "provider_environment": provider_environment,
            "subscription_status": (
                existing_subscription["status"] if existing_subscription else None
            ),
        }
        await conn.execute(
            """
            INSERT INTO billing_events (
                tenant_id, subscription_id, event_type, amount, currency, metadata
            )
            VALUES ($1, $2, 'payment_reconciliation_required', $3, 'COP', $4)
            """,
            attempt["tenant_id"],
            existing_subscription["id"] if existing_subscription else None,
            Decimal(amount_in_cents) / Decimal("100"),
            json.dumps(reconciliation_metadata),
        )
        await conn.execute(
            """
            UPDATE billing_payment_attempts
            SET status = 'approved', provider_transaction_id = $2,
                resolved_at = now(), updated_at = now()
            WHERE id = $1
            """,
            attempt["id"],
            transaction_id,
        )
        logger.error(
            "Onboarding payment requires reconciliation: tenant=%s attempt=%s "
            "transaction=%s subscription_status=%s",
            attempt["tenant_id"],
            attempt["id"],
            transaction_id,
            existing_subscription["status"] if existing_subscription else None,
        )
        return {"handled": True, "tenant_info": None}

    if existing_subscription is not None and existing_subscription["status"] != "pending":
        raise HTTPException(status_code=409, detail="Tenant subscription activation conflict")

    period_anchor = parse_wompi_period_anchor(transaction)
    subscription = await conn.fetchrow(
        """
        INSERT INTO tenant_subscriptions (
            tenant_id, plan_id, billing_cycle, status, gateway_reference,
            current_period_start, current_period_end
        )
        VALUES ($1, $2, 'annual', 'active', $3, $4, $4 + interval '1 year')
        ON CONFLICT (tenant_id) DO UPDATE SET
            plan_id = EXCLUDED.plan_id,
            billing_cycle = 'annual',
            status = 'active',
            gateway_reference = EXCLUDED.gateway_reference,
            current_period_start = EXCLUDED.current_period_start,
            current_period_end = EXCLUDED.current_period_end,
            cancelled_at = NULL,
            updated_at = now()
        WHERE tenant_subscriptions.status = 'pending'
        RETURNING id, current_period_end
        """,
        attempt["tenant_id"],
        attempt["plan_id"],
        provider_reference,
        period_anchor,
    )
    if subscription is None:
        raise HTTPException(status_code=409, detail="Tenant subscription activation conflict")

    metadata = {
        "payment_attempt_id": str(attempt["id"]),
        "wompi_transaction_id": transaction_id,
        "gateway_reference": provider_reference,
        "plan_id": str(attempt["plan_id"]),
        "provider_environment": provider_environment,
    }
    await conn.execute(
        """
        INSERT INTO billing_events (
            tenant_id, subscription_id, event_type, amount, currency, metadata
        )
        VALUES ($1, $2, 'payment_approved', $3, 'COP', $4)
        """,
        attempt["tenant_id"],
        subscription["id"],
        Decimal(amount_in_cents) / Decimal("100"),
        json.dumps(metadata),
    )
    await conn.execute(
        """
        UPDATE billing_payment_attempts
        SET status = 'approved', provider_transaction_id = $2,
            resolved_at = now(), updated_at = now()
        WHERE id = $1
        """,
        attempt["id"],
        transaction_id,
    )

    return {
        "handled": True,
        "tenant_info": {
            "tenant_id": str(attempt["tenant_id"]),
            "subscription_id": str(subscription["id"]),
            "tenant_name": identity["tenant_name"],
            "tenant_email": identity["tenant_email"],
            "plan_name": attempt["plan_name"],
            "next_period_end": subscription["current_period_end"].isoformat(),
        },
    }


async def process_paddle_onboarding_payment(
    conn,
    *,
    attempt_id: UUID,
    transaction_id: str,
    amount_minor: int,
    currency: str,
    period_anchor: Optional[datetime] = None,
    provider_environment: str = "prod",
    paddle_subscription_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Activate paid onboarding from a verified Paddle transaction (#795).

    Looks up billing_payment_attempts by attempt_id + provider=paddle.
    """
    txn_id = str(transaction_id or "").strip()
    if not txn_id:
        return {"handled": False, "activated": False, "reason": "missing_txn"}

    attempt = await conn.fetchrow(
        """
        SELECT a.id, a.tenant_id, a.plan_id, a.provider_reference,
               a.expected_amount_in_cents, a.currency, a.status,
               a.provider_transaction_id, a.provider_environment,
               p.name AS plan_name, p.is_active AS plan_is_active
        FROM billing_payment_attempts a
        JOIN subscription_plans p ON p.id = a.plan_id
        WHERE a.id = $1
          AND a.provider = 'paddle'
        FOR UPDATE OF a
        """,
        attempt_id,
    )
    if attempt is None:
        return {"handled": False, "activated": False, "reason": "attempt_not_found"}
    if attempt["provider_environment"] != provider_environment:
        raise HTTPException(status_code=409, detail="Paddle environment mismatch")

    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", txn_id)
    duplicate_attempt_id = await conn.fetchval(
        """
        SELECT id
        FROM billing_payment_attempts
        WHERE provider_transaction_id = $1
          AND provider_environment = $2
          AND id <> $3
        LIMIT 1
        """,
        txn_id,
        provider_environment,
        attempt["id"],
    )
    if duplicate_attempt_id is not None:
        raise HTTPException(status_code=409, detail="Paddle transaction ID mismatch")

    if attempt["status"] == "approved":
        if attempt["provider_transaction_id"] != txn_id:
            raise HTTPException(status_code=409, detail="Paddle transaction ID mismatch")
        return {"handled": True, "activated": False, "reason": "already_approved"}

    expected_currency = str(attempt["currency"]).strip().upper()
    currency_norm = str(currency or "").strip().upper()
    expected_amount = int(attempt["expected_amount_in_cents"])
    if (
        currency_norm != expected_currency
        or int(amount_minor) != expected_amount
        or not attempt["plan_is_active"]
    ):
        raise HTTPException(status_code=409, detail="Paddle payment evidence mismatch")

    existing_subscription = await conn.fetchrow(
        """
        SELECT id, status
        FROM tenant_subscriptions
        WHERE tenant_id = $1
        FOR UPDATE
        """,
        attempt["tenant_id"],
    )
    identity = await onboarding_service.activate_paid_onboarding_identity(
        conn, attempt["tenant_id"]
    )
    if identity is None:
        await conn.execute(
            """
            UPDATE billing_payment_attempts
            SET status = 'approved', provider_transaction_id = $2,
                resolved_at = now(), updated_at = now()
            WHERE id = $1
            """,
            attempt["id"],
            txn_id,
        )
        logger.error(
            "Paddle onboarding payment requires reconciliation: tenant=%s attempt=%s txn=%s",
            attempt["tenant_id"],
            attempt["id"],
            txn_id,
        )
        return {"handled": True, "activated": False, "reason": "reconciliation_required"}

    if existing_subscription is not None and existing_subscription["status"] != "pending":
        raise HTTPException(status_code=409, detail="Tenant subscription activation conflict")

    anchor = period_anchor or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    gateway_reference = str(attempt["provider_reference"] or txn_id)

    subscription = await conn.fetchrow(
        """
        INSERT INTO tenant_subscriptions (
            tenant_id, plan_id, billing_cycle, status, gateway_reference,
            current_period_start, current_period_end
        )
        VALUES ($1, $2, 'annual', 'active', $3, $4, $4 + interval '1 year')
        ON CONFLICT (tenant_id) DO UPDATE SET
            plan_id = EXCLUDED.plan_id,
            billing_cycle = 'annual',
            status = 'active',
            gateway_reference = EXCLUDED.gateway_reference,
            current_period_start = EXCLUDED.current_period_start,
            current_period_end = EXCLUDED.current_period_end,
            cancelled_at = NULL,
            updated_at = now()
        WHERE tenant_subscriptions.status = 'pending'
        RETURNING id, current_period_end
        """,
        attempt["tenant_id"],
        attempt["plan_id"],
        gateway_reference,
        anchor,
    )
    if subscription is None:
        raise HTTPException(status_code=409, detail="Tenant subscription activation conflict")

    metadata = {
        "payment_attempt_id": str(attempt["id"]),
        "paddle_transaction_id": txn_id,
        "gateway_reference": gateway_reference,
        "plan_id": str(attempt["plan_id"]),
        "provider": "paddle",
        "provider_environment": provider_environment,
    }
    if paddle_subscription_id:
        metadata["paddle_subscription_id"] = paddle_subscription_id

    await conn.execute(
        """
        INSERT INTO billing_events (
            tenant_id, subscription_id, event_type, amount, currency, metadata
        )
        VALUES ($1, $2, 'payment_approved', $3, $4, $5)
        """,
        attempt["tenant_id"],
        subscription["id"],
        Decimal(amount_minor) / Decimal("100"),
        expected_currency,
        json.dumps(metadata),
    )
    await conn.execute(
        """
        UPDATE billing_payment_attempts
        SET status = 'approved', provider_transaction_id = $2,
            resolved_at = now(), updated_at = now()
        WHERE id = $1
        """,
        attempt["id"],
        txn_id,
    )

    return {
        "handled": True,
        "activated": True,
        "tenant_info": {
            "tenant_id": str(attempt["tenant_id"]),
            "subscription_id": str(subscription["id"]),
            "tenant_name": identity["tenant_name"],
            "tenant_email": identity["tenant_email"],
            "plan_name": attempt["plan_name"],
            "next_period_end": subscription["current_period_end"].isoformat(),
        },
    }


async def activate_tenant_subscription(
    conn,
    gateway_reference: str,
    payment_id: str = "",
    amount: float = 0,
    currency: str = "COP",
    period_anchor: Optional[datetime] = None,
    expected_tenant_id: Optional[UUID] = None,
    amount_in_cents: Optional[int] = None,
):
    """
    Llamado desde el webhook de Wompi cuando la transacción es APPROVED.
    Activa la suscripción y extiende el período según billing_cycle.
    Retorna tenant_info dict o None si no hay fila pending/past_due.
    """
    row = await conn.fetchrow("""
        SELECT ts.id, ts.tenant_id, ts.billing_cycle,
               t.name AS tenant_name, t.email AS tenant_email,
               sp.name AS plan_name, sp.price_annual
        FROM tenant_subscriptions ts
        JOIN tenants t ON t.id = ts.tenant_id
        JOIN subscription_plans sp ON sp.id = ts.plan_id
        WHERE ts.gateway_reference = $1
          AND ts.status IN ('pending', 'past_due')
          AND ($2::uuid IS NULL OR ts.tenant_id = $2)
    """, gateway_reference, expected_tenant_id)

    if row is None:
        logger.warning(
            "activate_tenant_subscription: no pending/past_due row for gateway_reference=%s",
            gateway_reference,
        )
        return None

    if amount_in_cents is not None:
        expected_amount_in_cents = annual_price_in_cents(row["price_annual"])
        if (
            currency.upper() != "COP"
            or amount_in_cents != expected_amount_in_cents
        ):
            raise HTTPException(
                status_code=409,
                detail="La transacción no coincide con el valor de la suscripción",
            )

    if await payment_approved_exists(conn, row["id"], payment_id):
        logger.info(
            "activate_tenant_subscription: duplicate wompi_transaction_id=%s ref=%s — skipped",
            payment_id,
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
        period_anchor=period_anchor,
    )
    await onboarding_service.activate_paid_onboarding_identity(conn, row["tenant_id"])
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
      starter          — no paid subscription; permanent free Starter plan
      free             — legacy alias; treated as starter for access
      full             — active subscription
      full_with_warning — past_due, < 3 days overdue — access OK but banner shown
      read_only        — past_due, 3-7 days overdue — IA scanner blocked
      blocked          — pending checkout, past_due > 7 days, or cancelled/expired
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
    sub = await conn.fetchrow("""
        SELECT status, current_period_end, plan_id
        FROM tenant_subscriptions
        WHERE tenant_id = $1
    """, tenant_id)

    if sub is None:
        onboarding_state = await conn.fetchval(
            "SELECT state FROM tenant_onboarding WHERE tenant_id = $1",
            tenant_id,
        )
        if onboarding_state == "payment_pending":
            return SubscriptionAccess(
                level="blocked",
                grace_days_remaining=0,
                subscription_status="payment_pending",
                next_payment_date=None,
                message=(
                    "Ve a Mi Plan, elige una suscripción y completa el pago "
                    "para activar los módulos de WARO."
                ),
            )
        return SubscriptionAccess(
            level="starter",
            grace_days_remaining=0,
            subscription_status=None,
            next_payment_date=None,
            message="Estás en el plan Starter con límites operativos gratuitos.",
        )

    status = sub["status"]
    period_end = sub["current_period_end"]

    if status == "active":
        return SubscriptionAccess(
            level="full",
            grace_days_remaining=0,
            subscription_status=status,
            next_payment_date=period_end.date().isoformat() if period_end else None,
            message="Acceso completo.",
        )

    if status == "pending":
        return SubscriptionAccess(
            level="blocked",
            grace_days_remaining=0,
            subscription_status=status,
            next_payment_date=None,
            message="Completa el pago pendiente para activar tu suscripción.",
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
    Record a failed payment and set past_due only when grace applies.

    Grace (past_due) is allowed when the billing period has ended or the row is
    already past_due. Failed attempts while pending or while the period is still
    valid only append a billing event — they do not start the grace window.

    Returns tenant info dict for email trigger, or None if not found.
    """
    row = await conn.fetchrow("""
        SELECT ts.id AS subscription_id, ts.tenant_id, ts.status,
               ts.current_period_end
        FROM tenant_subscriptions ts
        WHERE ts.gateway_reference = $1
    """, gateway_reference)

    if row is None:
        logger.warning(
            "mark_subscription_past_due: no subscription for preapproval=%s",
            gateway_reference,
        )
        return None

    sub_id = row["subscription_id"]
    tenant_id = row["tenant_id"]
    status = row["status"]
    period_end = row["current_period_end"]
    if period_end is not None and period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    period_expired = period_end is not None and period_end < now
    should_mark_past_due = status == "past_due" or period_expired

    if should_mark_past_due:
        await conn.execute("""
            UPDATE tenant_subscriptions
            SET status = 'past_due', updated_at = now()
            WHERE id = $1
        """, sub_id)
    else:
        logger.info(
            "mark_subscription_past_due: skipped past_due status=%s period_end=%s ref=%s",
            status,
            period_end,
            gateway_reference,
        )

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
