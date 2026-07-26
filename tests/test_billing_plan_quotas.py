from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.services import billing_service


def _plan_row(slug: str, features: dict):
    now = datetime.now(timezone.utc)
    return {
        "id": uuid4(),
        "name": slug,
        "slug": slug,
        "description": None,
        "price_monthly": 0,
        "price_annual": 0,
        "scan_limit": 500,
        "is_active": True,
        "features": features,
        "created_at": now,
        "updated_at": now,
    }


def test_serialize_plan_exposes_pro_quota_defaults():
    plan = billing_service._serialize_plan(_plan_row("pro", {"support": True}))

    assert plan["features"] == {"support": True}
    assert plan["quotas"]["admin_users"] == 6
    assert plan["quotas"]["active_sessions_per_admin_user"] == 1
    assert plan["quotas"]["active_kitchens"] == 2
    assert plan["quotas"]["active_tables_including_bar"] == 20
    assert plan["quotas"]["active_qr_tables"] == 20
    assert plan["quotas"]["completed_online_orders_per_month"] == 300
    assert plan["quotas"]["electronic_invoices_per_period"] == 0


def test_serialize_plan_keeps_facturacion_legacy_invoice_limit():
    plan = billing_service._serialize_plan(
        _plan_row(
            "facturacion-electronica",
            {"electronic_invoice_limit": 200},
        )
    )

    assert plan["quotas"]["electronic_invoices_per_period"] == 200


def test_serialize_plan_prefers_structured_quota_values():
    plan = billing_service._serialize_plan(
        _plan_row(
            "facturacion-electronica",
            {
                "electronic_invoice_limit": 200,
                "quotas": {
                    "admin_users": 8,
                    "electronic_invoices_per_period": 300,
                },
            },
        )
    )

    assert plan["quotas"]["admin_users"] == 8
    assert plan["quotas"]["active_kitchens"] == 2
    assert plan["quotas"]["electronic_invoices_per_period"] == 300


@pytest.mark.asyncio
async def test_remaining_usage_exposes_quota_usage_and_internal_roles_only():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "current_period_start": period_start,
                "current_period_end": period_end,
                "plan_slug": "facturacion-electronica",
                "plan_features": {
                    "quotas": {
                        "admin_users": 6,
                        "active_sessions_per_admin_user": 1,
                        "active_kitchens": 2,
                        "active_tables_including_bar": 20,
                        "active_qr_tables": 20,
                        "completed_online_orders_per_month": 300,
                        "electronic_invoices_per_period": 200,
                    }
                },
                "plan_scan_limit": 500,
                "scans_used": 25,
                "scans_limit": 500,
            },
            {
                "admin_users": 4,
                "active_sessions_per_admin_user": 1,
                "active_kitchens": 2,
                "active_tables_including_bar": 7,
                "active_qr_tables": 6,
                "completed_online_orders_per_month": 28,
                "menu_products": 9,
                "menu_categories": 3,
                "tenant_ingredients": 0,
                "tenant_suppliers": 0,
                "direct_purchases_per_period": 0,
                "stock_adjustments_per_period": 0,
                "modifier_groups": 1,
                "recipe_bases": 2,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=12)
    conn.fetch = AsyncMock(return_value=[])

    usage = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert usage["quota_usage"]["admin_users"] == {
        "used": 4,
        "limit": 6,
        "remaining": 2,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
    assert usage["quota_usage"]["completed_online_orders_per_month"]["used"] == 28
    assert usage["quota_usage"]["electronic_invoices_per_period"]["used"] == 12
    assert usage["quota_usage"]["electronic_invoices_per_period"]["limit"] == 200
    assert usage["quota_usage"]["menu_products"] == {
        "used": 9,
        "limit": 1_000_000,
        "remaining": 1_000_000 - 9,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
    assert usage["quota_usage"]["menu_categories"]["used"] == 3
    assert usage["quota_usage"]["menu_categories"]["limit"] == 1_000_000
    assert usage["quota_usage"]["modifier_groups"]["used"] == 1
    assert usage["quota_usage"]["modifier_groups"]["limit"] == 1_000_000
    assert usage["quota_usage"]["recipe_bases"]["used"] == 2
    assert usage["quota_usage"]["recipe_bases"]["limit"] == 1_000_000

    quota_query_args = conn.fetchrow.await_args_list[1].args
    assert quota_query_args[4] == list(LEGACY_INTERNAL_TEAM_ROLES)
    assert "customer" not in quota_query_args[4]


@pytest.mark.asyncio
async def test_remaining_usage_exposes_effective_quota_override_state():
    tenant_id = uuid4()
    override_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "current_period_start": period_start,
                "current_period_end": period_end,
                "plan_slug": "pro",
                "plan_features": {"quotas": {"active_kitchens": 2}},
                "plan_scan_limit": 500,
                "scans_used": 0,
                "scans_limit": 500,
            },
            {
                "admin_users": 4,
                "active_sessions_per_admin_user": 1,
                "active_kitchens": 3,
                "active_tables_including_bar": 7,
                "active_qr_tables": 6,
                "completed_online_orders_per_month": 28,
                "menu_products": 0,
                "menu_categories": 0,
                "tenant_ingredients": 0,
                "tenant_suppliers": 0,
                "direct_purchases_per_period": 0,
                "stock_adjustments_per_period": 0,
                "modifier_groups": 0,
                "recipe_bases": 0,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[{
        "id": override_id,
        "resource": "active_kitchens",
        "limit_override": 4,
        "disabled": False,
        "reason": "Commercial exception",
    }])

    usage = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert usage["quota_usage"]["active_kitchens"] == {
        "used": 3,
        "limit": 4,
        "remaining": 1,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "plan_limit": 2,
        "override": {
            "id": str(override_id),
            "disabled": False,
            "reason": "Commercial exception",
        },
    }


@pytest.mark.asyncio
async def test_remaining_usage_exposes_disabled_override_as_unlimited():
    tenant_id = uuid4()
    override_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "current_period_start": period_start,
                "current_period_end": period_end,
                "plan_slug": "pro",
                "plan_features": {"quotas": {"completed_online_orders_per_month": 300}},
                "plan_scan_limit": 500,
                "scans_used": 0,
                "scans_limit": 500,
            },
            {
                "admin_users": 4,
                "active_sessions_per_admin_user": 1,
                "active_kitchens": 2,
                "active_tables_including_bar": 7,
                "active_qr_tables": 6,
                "completed_online_orders_per_month": 301,
                "menu_products": 0,
                "menu_categories": 0,
                "tenant_ingredients": 0,
                "tenant_suppliers": 0,
                "direct_purchases_per_period": 0,
                "stock_adjustments_per_period": 0,
                "modifier_groups": 0,
                "recipe_bases": 0,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[{
        "id": override_id,
        "resource": "completed_online_orders_per_month",
        "limit_override": None,
        "disabled": True,
        "reason": "Pilot",
    }])

    usage = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert usage["quota_usage"]["completed_online_orders_per_month"]["limit"] is None
    assert usage["quota_usage"]["completed_online_orders_per_month"]["remaining"] is None
    assert usage["quota_usage"]["completed_online_orders_per_month"]["plan_limit"] == 300
    assert usage["quota_usage"]["completed_online_orders_per_month"]["override"] == {
        "id": str(override_id),
        "disabled": True,
        "reason": "Pilot",
    }
