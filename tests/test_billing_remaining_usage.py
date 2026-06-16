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


@pytest.mark.asyncio
async def test_remaining_usage_non_fe_plan_reports_zero_invoice_quota():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(plan_slug="pro", scans_used=12, scans_limit=500)
    )
    conn.fetchval = AsyncMock()

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
    subscription_query = conn.fetchrow.await_args.args[0]
    assert "su.period_start <= now()" in subscription_query
    assert "su.period_end > now()" in subscription_query
    assert "sp.features AS plan_features" in subscription_query
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_remaining_usage_non_fe_plan_ignores_invoice_feature_metadata():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(
            plan_slug="pro",
            plan_features={"electronic_invoice_limit": 200},
        )
    )
    conn.fetchval = AsyncMock()

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["electronic_invoice_usage"]["used"] == 0
    assert result["electronic_invoice_usage"]["limit"] == 0
    assert result["electronic_invoice_usage"]["remaining"] == 0
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_remaining_usage_fe_plan_counts_accepted_invoices_in_period():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(
            plan_slug="facturacion-electronica",
            plan_features={"electronic_invoice_limit": 200},
            scans_used=20,
            scans_limit=500,
        )
    )
    conn.fetchval = AsyncMock(return_value=37)

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
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(
            plan_slug="facturacion-electronica",
            plan_features={"electronic_invoice_limit": "150"},
        )
    )
    conn.fetchval = AsyncMock(return_value=12)

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["electronic_invoice_usage"]["used"] == 12
    assert result["electronic_invoice_usage"]["limit"] == 150
    assert result["electronic_invoice_usage"]["remaining"] == 138


@pytest.mark.asyncio
async def test_remaining_usage_caps_remaining_at_zero():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(
            plan_slug="facturacion-electronica",
            plan_features={"electronic_invoice_limit": 200},
            scans_used=550,
            scans_limit=500,
        )
    )
    conn.fetchval = AsyncMock(return_value=220)

    result = await billing_service.get_remaining_billing_usage(conn, tenant_id)

    assert result["scan_usage"]["remaining"] == 0
    assert result["electronic_invoice_usage"]["remaining"] == 0


@pytest.mark.asyncio
async def test_remaining_usage_missing_subscription_mirrors_subscription_endpoint():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await billing_service.get_remaining_billing_usage(conn, uuid4())

    assert exc.value.status_code == 404
    assert exc.value.detail == "Subscription not found"
