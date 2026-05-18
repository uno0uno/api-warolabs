"""Unit tests for cierre shift-template resolution (warocol.com#686)."""
from datetime import date, datetime, time
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import APIError
from app.services.cierre_service import resolve_cierre_period_fields

BOG = ZoneInfo("America/Bogota")


@pytest.mark.asyncio
async def test_template_mode_resolves_window():
    template_id = uuid4()
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": template_id,
        "name": "Mañana",
        "start_time": time(6, 0),
        "end_time": time(14, 0),
        "crosses_midnight": False,
    }

    resolved = await resolve_cierre_period_fields(
        conn,
        tenant_id,
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 18),
        period_start_time=None,
        period_end_time=None,
        shift_template_id=template_id,
    )

    assert resolved.shift_template_id == template_id
    assert resolved.period_start == date(2026, 5, 18)
    assert resolved.period_end == date(2026, 5, 18)
    assert resolved.period_start_time == datetime(2026, 5, 18, 6, 0, tzinfo=BOG)
    assert resolved.period_end_time == datetime(2026, 5, 18, 14, 0, tzinfo=BOG)


@pytest.mark.asyncio
async def test_template_mode_rejects_manual_times():
    conn = AsyncMock()
    with pytest.raises(APIError) as exc:
        await resolve_cierre_period_fields(
            conn,
            uuid4(),
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 18),
            period_start_time=datetime(2026, 5, 18, 15, 0, tzinfo=BOG),
            period_end_time=datetime(2026, 5, 18, 18, 0, tzinfo=BOG),
            shift_template_id=uuid4(),
        )
    assert exc.value.status_code == 422
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_template_mode_rejects_multi_day():
    conn = AsyncMock()
    with pytest.raises(APIError) as exc:
        await resolve_cierre_period_fields(
            conn,
            uuid4(),
            period_start=date(2026, 5, 17),
            period_end=date(2026, 5, 18),
            period_start_time=None,
            period_end_time=None,
            shift_template_id=uuid4(),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_custom_mode_passes_through_times():
    tenant_id = uuid4()
    start = datetime(2026, 5, 18, 15, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 18, 0, tzinfo=BOG)
    conn = AsyncMock()

    resolved = await resolve_cierre_period_fields(
        conn,
        tenant_id,
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 18),
        period_start_time=start,
        period_end_time=end,
        shift_template_id=None,
    )

    assert resolved.shift_template_id is None
    assert resolved.period_start_time == start
    assert resolved.period_end_time == end
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_full_day_mode_no_times():
    resolved = await resolve_cierre_period_fields(
        AsyncMock(),
        uuid4(),
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 18),
        period_start_time=None,
        period_end_time=None,
        shift_template_id=None,
    )
    assert resolved.period_start_time is None
    assert resolved.period_end_time is None
