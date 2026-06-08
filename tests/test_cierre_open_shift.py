"""Unit tests for shift opening cash float helpers (warocol.com#920)."""
from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import APIError
from app.models.cierre import CierreCreate
from app.services.cierre_service import (
    _effective_period_bounds,
    _is_day_only_cierre_request,
    _open_shift_has_explicit_window,
    _requires_open_shift,
    create_cierre,
)

BOG = ZoneInfo("America/Bogota")


def test_effective_period_bounds_uses_timestamps_when_present():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18), date(2026, 5, 18), start, end,
    )
    assert eff_start == start
    assert eff_end == end


def test_effective_period_bounds_full_day_bogota():
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18), date(2026, 5, 18), None, None,
    )
    assert eff_start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=BOG)
    assert eff_end == datetime(2026, 5, 18, 23, 59, 59, tzinfo=BOG)


def test_requires_open_shift_template_mode():
    assert _requires_open_shift(uuid4(), None, None) is True


def test_requires_open_shift_custom_times():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    assert _requires_open_shift(None, start, end) is True


def test_day_only_cierre_request_detected():
    assert _is_day_only_cierre_request(None, None, None) is True


def test_open_shift_has_explicit_window_for_template_or_times():
    assert _open_shift_has_explicit_window({
        "shift_template_id": uuid4(),
        "period_start_time": None,
        "period_end_time": None,
    }) is True
    assert _open_shift_has_explicit_window({
        "shift_template_id": None,
        "period_start_time": datetime(2026, 6, 6, 0, 0, tzinfo=BOG),
        "period_end_time": datetime(2026, 6, 6, 4, 1, tzinfo=BOG),
    }) is True
    assert _open_shift_has_explicit_window({
        "shift_template_id": None,
        "period_start_time": None,
        "period_end_time": None,
    }) is False


@pytest.mark.asyncio
async def test_create_cierre_rejects_day_only_when_open_shift_overlaps():
    tenant_id = uuid4()
    shift_id = uuid4()
    conn = AsyncMock()

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    open_shift = {
        "id": uuid4(),
        "opening_cash": 100_000,
        "shift_template_id": shift_id,
        "period_start": date(2026, 6, 6),
        "period_end": date(2026, 6, 6),
        "period_start_time": datetime(2026, 6, 6, 0, 0, tzinfo=BOG),
        "period_end_time": datetime(2026, 6, 6, 4, 1, tzinfo=BOG),
    }
    body = CierreCreate(
        periodStart=date(2026, 6, 6),
        periodEnd=date(2026, 6, 6),
        cashCounted=100_000,
    )

    with patch(
        "app.services.cierre_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.cierre_service.get_db_connection",
        side_effect=db_ctx,
    ), patch(
        "app.services.cierre_service._find_overlapping_period_id",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.cierre_service._fetch_open_shift_for_window",
        new=AsyncMock(return_value=open_shift),
    ), patch(
        "app.services.cierre_service._compute_preview",
        new=AsyncMock(),
    ) as compute_preview:
        with pytest.raises(APIError) as exc:
            await create_cierre(AsyncMock(), body)

    assert exc.value.status_code == 422
    assert "turno de caja abierto" in exc.value.message
    compute_preview.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()
    conn.execute.assert_not_awaited()
