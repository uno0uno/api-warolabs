"""Operational TZ second stage for WaRos analytics buckets (warocol.com#1600)."""
from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services.waros_service import _get_waros_analytics_for_tenant


def _summary_row():
    return {
        "total_issued": 0,
        "total_redeemed": 0,
        "active_members": 0,
    }


@pytest.mark.asyncio
async def test_waros_day_analytics_uses_resolved_tenant_timezone_in_sql():
    tenant_id = str(uuid4())
    fetch_calls: list[tuple] = []

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_summary_row())

    async def _fetch(sql, *args):
        fetch_calls.append((sql, args))
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)

    @asynccontextmanager
    async def _ctx(*_, **__):
        yield conn

    with patch("app.services.waros_service.get_db_connection", side_effect=_ctx), patch(
        "app.services.waros_service.resolve_tenant_timezone",
        new=AsyncMock(return_value="America/Mexico_City"),
    ):
        result = await _get_waros_analytics_for_tenant(
            tenant_id,
            group_by="day",
            date_from=None,
            date_to=None,
        )

    assert result["group_by"] == "day"
    assert result["groups"] == []
    assert len(fetch_calls) == 1
    sql, args = fetch_calls[0]
    assert "America/Bogota" not in sql
    assert "AT TIME ZONE $" in sql
    assert args[0] == tenant_id
    assert args[1] == "America/Mexico_City"


@pytest.mark.asyncio
async def test_waros_week_analytics_binds_tenant_timezone_after_date_filters():
    tenant_id = str(uuid4())
    fetch_calls: list[tuple] = []

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_summary_row())

    async def _fetch(sql, *args):
        fetch_calls.append((sql, args))
        period = datetime(2026, 7, 6, tzinfo=ZoneInfo("UTC"))
        return [
            {
                "period": period,
                "total_earned": 10,
                "total_redeemed": 2,
                "active_members": 1,
            }
        ]

    conn.fetch = AsyncMock(side_effect=_fetch)

    @asynccontextmanager
    async def _ctx(*_, **__):
        yield conn

    with patch("app.services.waros_service.get_db_connection", side_effect=_ctx), patch(
        "app.services.waros_service.resolve_tenant_timezone",
        new=AsyncMock(return_value="America/New_York"),
    ):
        result = await _get_waros_analytics_for_tenant(
            tenant_id,
            group_by="week",
            date_from="2026-07-01",
            date_to="2026-07-10",
        )

    assert result["groups"][0]["period"] == date(2026, 7, 6).isoformat()
    sql, args = fetch_calls[0]
    assert "date_trunc('week'" in sql
    assert "DATE(created_at AT TIME ZONE $2)" in sql
    assert "date_trunc('week', created_at AT TIME ZONE $2)" in sql.replace("\n", " ")
    assert args[0] == tenant_id
    assert args[1] == "America/New_York"
    assert date(2026, 7, 1) in args
    assert date(2026, 7, 10) in args
