from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import billing_service


def _subscription_row(
    *,
    plan_slug: str,
    plan_features=None,
    scans_used: int = 0,
    scans_limit: int = 500,
):
    return {
        "current_period_start": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "current_period_end": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "plan_slug": plan_slug,
        "plan_features": plan_features or {},
        "plan_scan_limit": scans_limit,
        "scans_used": scans_used,
        "scans_limit": scans_limit,
    }


def _quota_counts_row(**overrides):
    base = {
        "admin_users": 0,
        "active_sessions_per_admin_user": 0,
        "active_kitchens": 0,
        "active_tables_including_bar": 0,
        "active_qr_tables": 0,
        "completed_online_orders_per_month": 0,
        "menu_products": 0,
        "menu_categories": 0,
        "tenant_ingredients": 0,
        "modifier_groups": 0,
        "recipe_bases": 0,
    }
    base.update(overrides)
    return base


def _mock_remaining_usage_conn(subscription_row, *, invoice_used=0):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[subscription_row, _quota_counts_row()])
    conn.fetchval = AsyncMock(return_value=invoice_used)
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest.mark.asyncio
async def test_remaining_usage_non_fe_plan_reports_zero_invoice_quota():
    tenant_id = uuid4()
    conn = _mock_remaining_usage_conn(
        _subscription_row(plan_slug="pro", scans_used=12, scans_limit=500)
    )

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["scan_usage"] == {
        "used": 12,
        "limit": 500,
        "remaining": 488,
        "period_start": "2026-06-01T00:00:00+00:00",
        "period_end": "2026-07-01T00:00:00+00:00",
    }
    assert result["electronic_invoice_usage"] == {
        "used": 0,
        "limit": 0,
        "remaining": 0,
        "period_start": "2026-06-01T00:00:00+00:00",
        "period_end": "2026-07-01T00:00:00+00:00",
    }
    assert result["quota_usage"]["menu_products"]["limit"] == 1_000_000
    assert result["quota_usage"]["menu_categories"]["limit"] == 1_000_000
    assert result["quota_usage"]["modifier_groups"]["limit"] == 1_000_000
    assert result["quota_usage"]["recipe_bases"]["limit"] == 1_000_000
    assert result["quota_usage"]["recipe_lines_per_product"]["used"] == 0
    assert result["quota_usage"]["recipe_lines_per_product"]["limit"] == 100
    assert result["quota_usage"]["modifier_options_per_group"]["used"] == 0
    assert result["quota_usage"]["modifier_options_per_group"]["limit"] == 50
    subscription_query = conn.fetchrow.await_args_list[0].args[0]
    assert "su.period_start <= now()" in subscription_query
    assert "su.period_end > now()" in subscription_query
    assert "sp.features AS plan_features" in subscription_query
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_remaining_usage_non_fe_plan_ignores_invoice_feature_metadata():
    tenant_id = uuid4()
    conn = _mock_remaining_usage_conn(
        _subscription_row(
            plan_slug="pro",
            plan_features={"electronic_invoice_limit": 200},
        )
    )

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["electronic_invoice_usage"]["used"] == 0
    assert result["electronic_invoice_usage"]["limit"] == 0
    assert result["electronic_invoice_usage"]["remaining"] == 0
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_remaining_usage_fe_plan_counts_accepted_invoices_in_period():
    tenant_id = uuid4()
    conn = _mock_remaining_usage_conn(
        _subscription_row(
            plan_slug="facturacion-electronica",
            plan_features={"electronic_invoice_limit": 200},
            scans_used=20,
            scans_limit=500,
        ),
        invoice_used=37,
    )

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["electronic_invoice_usage"] == {
        "used": 37,
        "limit": 200,
        "remaining": 163,
        "period_start": "2026-06-01T00:00:00+00:00",
        "period_end": "2026-07-01T00:00:00+00:00",
    }

    invoice_query, query_tenant_id, period_start, period_end = conn.fetchval.await_args.args
    assert query_tenant_id == tenant_id
    assert period_start == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert period_end == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert "tenant_id = $1" in invoice_query
    assert "status = 'accepted'" in invoice_query
    assert "document_type = 'invoice'" in invoice_query
    assert "COALESCE(emitted_at, created_at) >= $2" in invoice_query
    assert "COALESCE(emitted_at, created_at) < $3" in invoice_query


@pytest.mark.asyncio
async def test_remaining_usage_fe_plan_reads_numeric_invoice_limit_from_features():
    tenant_id = uuid4()
    conn = _mock_remaining_usage_conn(
        _subscription_row(
            plan_slug="facturacion-electronica",
            plan_features={"electronic_invoice_limit": "150"},
        ),
        invoice_used=12,
    )

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["electronic_invoice_usage"]["used"] == 12
    assert result["electronic_invoice_usage"]["limit"] == 150
    assert result["electronic_invoice_usage"]["remaining"] == 138


@pytest.mark.asyncio
async def test_remaining_usage_caps_remaining_at_zero():
    tenant_id = uuid4()
    conn = _mock_remaining_usage_conn(
        _subscription_row(
            plan_slug="facturacion-electronica",
            plan_features={"electronic_invoice_limit": 200},
            scans_used=550,
            scans_limit=500,
        ),
        invoice_used=220,
    )

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["scan_usage"]["remaining"] == 0
    assert result["electronic_invoice_usage"]["remaining"] == 0


@pytest.mark.asyncio
async def test_remaining_usage_missing_subscription_mirrors_subscription_endpoint():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value="payment_pending")

    with pytest.raises(HTTPException) as exc:
        await billing_service.get_remaining_billing_usage(conn, uuid4())

    assert exc.value.status_code == 404
    assert exc.value.detail == "Subscription not found"


@pytest.mark.asyncio
async def test_remaining_usage_starter_without_subscription_exposes_catalog_quotas():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,  # no tenant_subscriptions row
            None,  # get_effective_plan_slug paid lookup
            {
                "current_period_start": period_start,
                "current_period_end": period_end,
                "plan_slug": "starter",
                "plan_features": {
                    "quotas": {
                        "menu_products": 10,
                        "menu_categories": 5,
                        "tenant_ingredients": 5,
                        "modifier_groups": 4,
                        "recipe_bases": 5,
                        "admin_users": 1,
                        "active_sessions_per_admin_user": 1,
                        "active_kitchens": 0,
                        "active_tables_including_bar": 0,
                        "active_qr_tables": 0,
                        "completed_online_orders_per_month": 30,
                        "electronic_invoices_per_period": 0,
                    }
                },
                "plan_scan_limit": 10,
                "scans_used": 3,
                "scans_limit": 10,
            },
            {
                "admin_users": 1,
                "active_sessions_per_admin_user": 1,
                "active_kitchens": 0,
                "active_tables_including_bar": 0,
                "active_qr_tables": 0,
                "completed_online_orders_per_month": 5,
                "menu_products": 10,
                "menu_categories": 5,
                "tenant_ingredients": 5,
                "modifier_groups": 4,
                "recipe_bases": 5,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value="starter_active")
    conn.fetch = AsyncMock(return_value=[])

    usage = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert usage["scan_usage"]["used"] == 3
    assert usage["scan_usage"]["limit"] == 10
    assert usage["quota_usage"]["menu_products"] == {
        "used": 10,
        "limit": 10,
        "remaining": 0,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
    assert usage["quota_usage"]["menu_categories"]["used"] == 5
    assert usage["quota_usage"]["menu_categories"]["remaining"] == 0
    assert usage["quota_usage"]["tenant_ingredients"]["used"] == 5
    assert usage["quota_usage"]["tenant_ingredients"]["limit"] == 5
    assert usage["quota_usage"]["tenant_ingredients"]["remaining"] == 0
    assert usage["quota_usage"]["modifier_groups"]["used"] == 4
    assert usage["quota_usage"]["modifier_groups"]["remaining"] == 0
    assert usage["quota_usage"]["recipe_bases"]["used"] == 5
    assert usage["quota_usage"]["recipe_bases"]["remaining"] == 0
    assert usage["quota_usage"]["recipe_lines_per_product"] == {
        "used": 0,
        "limit": 4,
        "remaining": 4,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
    assert usage["quota_usage"]["modifier_options_per_group"]["limit"] == 6
    assert usage["quota_usage"]["modifier_options_per_group"]["used"] == 0
