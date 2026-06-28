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
    _build_open_tables_filter,
    _build_order_date_filter,
    _effective_period_bounds,
    _is_day_only_cierre_request,
    _open_shift_has_explicit_window,
    _resolve_remaining_day_window_from_rows,
    _requires_open_shift,
    create_cierre,
)

BOG = ZoneInfo("America/Bogota")
LA = ZoneInfo("America/Los_Angeles")


def test_effective_period_bounds_uses_timestamps_when_present():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18), date(2026, 5, 18), start, end,
    )
    assert eff_start == start
    assert eff_end == end


def test_effective_period_bounds_full_day_default_timezone():
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18), date(2026, 5, 18), None, None,
    )
    assert eff_start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=BOG)
    assert eff_end == datetime(2026, 5, 18, 23, 59, 59, tzinfo=BOG)


def test_effective_period_bounds_full_day_tenant_timezone():
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18),
        date(2026, 5, 18),
        None,
        None,
        "America/Los_Angeles",
    )
    assert eff_start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=LA)
    assert eff_end == datetime(2026, 5, 18, 23, 59, 59, tzinfo=LA)


def test_date_only_order_filter_binds_tenant_timezone():
    sql, params = _build_order_date_filter(
        date(2026, 5, 18),
        date(2026, 5, 18),
        None,
        None,
        "America/Los_Angeles",
    )
    assert "order_date AT TIME ZONE $2" in sql
    assert params == ["America/Los_Angeles", date(2026, 5, 18), date(2026, 5, 18)]


def test_date_only_open_tables_filter_binds_tenant_timezone():
    sql, params = _build_open_tables_filter(
        date(2026, 5, 18),
        date(2026, 5, 18),
        None,
        None,
        "America/Los_Angeles",
    )
    assert "ts.opened_at AT TIME ZONE $2" in sql
    assert params == ["America/Los_Angeles", date(2026, 5, 18), date(2026, 5, 18)]


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


def test_remaining_day_window_without_existing_closes_returns_full_day():
    window = _resolve_remaining_day_window_from_rows(
        date(2026, 6, 29),
        [],
        timezone_name="Pacific/Kiritimati",
    )

    assert window.is_partial is False
    assert window.resolved.period_start == date(2026, 6, 29)
    assert window.resolved.period_end == date(2026, 6, 29)
    assert window.resolved.period_start_time is None
    assert window.resolved.period_end_time is None


def test_remaining_day_window_after_partial_close_starts_at_latest_end():
    kiritimati = ZoneInfo("Pacific/Kiritimati")
    row = {
        "period_start": date(2026, 6, 28),
        "period_end": date(2026, 6, 29),
        "period_start_time": datetime(2026, 6, 28, 18, 0, tzinfo=kiritimati),
        "period_end_time": datetime(2026, 6, 29, 10, 0, tzinfo=kiritimati),
    }

    window = _resolve_remaining_day_window_from_rows(
        date(2026, 6, 29),
        [row],
        timezone_name="Pacific/Kiritimati",
    )

    assert window.is_partial is True
    assert window.resolved.period_start == date(2026, 6, 29)
    assert window.resolved.period_end == date(2026, 6, 29)
    assert window.resolved.period_start_time == datetime(2026, 6, 29, 10, 0, tzinfo=kiritimati)
    assert window.resolved.period_end_time == datetime(2026, 6, 29, 23, 59, 59, tzinfo=kiritimati)


def test_remaining_day_window_rejects_fully_covered_day():
    kiritimati = ZoneInfo("Pacific/Kiritimati")
    row = {
        "period_start": date(2026, 6, 29),
        "period_end": date(2026, 6, 29),
        "period_start_time": datetime(2026, 6, 29, 0, 0, tzinfo=kiritimati),
        "period_end_time": datetime(2026, 6, 29, 23, 59, 59, tzinfo=kiritimati),
    }

    with pytest.raises(APIError) as exc:
        _resolve_remaining_day_window_from_rows(
            date(2026, 6, 29),
            [row],
            timezone_name="Pacific/Kiritimati",
        )

    assert exc.value.status_code == 409
    assert "completamente cubierto" in exc.value.message


@pytest.mark.asyncio
async def test_create_cierre_rejects_day_only_when_open_shift_overlaps():
    tenant_id = uuid4()
    shift_id = uuid4()
    conn = AsyncMock()
    conn.fetch.return_value = []

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


@pytest.mark.asyncio
async def test_create_cierre_rejects_rebel_rebel_open_table_before_writes():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetch.return_value = []

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 6, 6, 22, 35, tzinfo=BOG)
            return current if tz is None else current.astimezone(tz)

    preview = {
        "totalSales": 0.0,
        "itemsSold": 0,
        "totalTips": 0.0,
        "totalTipTax": 0.0,
        "totalCharged": 0.0,
        "cashTips": 0.0,
        "totalCash": 0.0,
        "totalCard": 0.0,
        "totalDigital": 0.0,
        "totalCredit": 0.0,
        "gastosEfectivo": 0.0,
        "cashExpected": 0.0,
        "openTablesCount": 1,
    }
    body = CierreCreate(
        periodStart=date(2026, 6, 6),
        periodEnd=date(2026, 6, 6),
        cashCounted=0,
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
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.cierre_service._compute_preview",
        new=AsyncMock(return_value=preview),
    ) as compute_preview, patch(
        "app.services.cierre_service.datetime",
        FrozenDateTime,
    ):
        with pytest.raises(APIError) as exc:
            await create_cierre(AsyncMock(), body)

    assert exc.value.status_code == 409
    assert "Hay 1 mesa(s) con cuenta abierta" in exc.value.message
    assert "Cierra todas las mesas antes de registrar el cierre del día" in exc.value.message
    compute_preview.assert_awaited_once()
    conn.fetchrow.assert_not_awaited()
    conn.execute.assert_not_awaited()
