"""Regression tests for duplicate-period handling in cierre create.

Issue: https://github.com/uno0uno/api-warolabs/issues/898

Two shifts per day (e.g. Waro_Colombia cafeteria + restaurante) must coexist.
A genuine duplicate (same tenant + same day + same shift_template_id) must surface
as a clean APIError(409) instead of an opaque 500.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app.core.exceptions import APIError
from app.models.cierre import CierreCreate
from app.services.cierre_service import create_cierre

BOG = ZoneInfo("America/Bogota")


def _unique_violation(constraint: str = "uq_period_tenant_active") -> asyncpg.UniqueViolationError:
    err = asyncpg.UniqueViolationError(
        "duplicate key value violates unique constraint \""
        + constraint
        + "\""
    )
    err.constraint_name = constraint
    err.detail = f"Key (...) already exists."
    return err


class _FakeConn:
    """Minimal asyncpg connection stub for create_cierre tests.

    create_cierre opens a transaction via get_db_connection(use_transaction=True);
    the only methods it touches during the duplicate-handling branch are the
    `async with` enter/exit and `fetchrow` for the INSERT into accounting_period.
    """

    def __init__(self, raise_on_insert: Exception | None = None):
        self._raise_on_insert = raise_on_insert
        self.inserted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchrow(self, query: str, *args):
        if "INSERT INTO accounting_period" in query:
            if self._raise_on_insert:
                raise self._raise_on_insert
            self.inserted = True
            return {"id": uuid4(), "closed_at": datetime(2026, 8, 23, 22, 0, tzinfo=BOG)}
        # Any other fetchrow in the duplicate branch returns empty.
        return None

    async def execute(self, query: str, *args):
        return "UPDATE 0"


def _fake_request(tenant_id):
    req = SimpleNamespace()
    req.headers = {}
    return req


@pytest.mark.asyncio
async def test_create_cierre_maps_duplicate_period_to_409(monkeypatch):
    """When the DB raises UniqueViolationError for uq_period_tenant_active,
    create_cierre must raise APIError(409) with a Spanish message."""
    tenant_id = uuid4()
    session = SimpleNamespace(tenant_id=tenant_id, user_id=uuid4())

    fake_conn = _FakeConn(raise_on_insert=_unique_violation())

    @asynccontextmanager
    async def fake_db_conn(use_transaction: bool = True):
        yield fake_conn

    monkeypatch.setattr(
        "app.services.cierre_service.get_db_connection",
        fake_db_conn,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.require_valid_session",
        lambda req: session,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_tenant_timezone",
        AsyncMock(return_value="America/Bogota"),
    )
    # Bypass all the upstream validation/preview by short-circuiting.
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_cierre_period_fields",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._resolve_day_only_period",
        AsyncMock(),
    )

    body = CierreCreate(
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        shift_template_id=uuid4(),
        cash_counted=1000.0,
        payment_breakdown_reported=None,
    )

    # The function unpacks `body` extensively before reaching the INSERT.
    # We patch the upstream helpers to return SafeNamespace so the flow
    # reaches the duplicate-INSERT without exploding earlier.
    resolved = SimpleNamespace(
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        period_start_time=datetime(2026, 8, 23, 17, 10, tzinfo=BOG),
        period_end_time=datetime(2026, 8, 23, 22, 30, tzinfo=BOG),
        shift_template_id=body.shift_template_id,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_cierre_period_fields",
        AsyncMock(return_value=resolved),
    )
    window = SimpleNamespace(resolved=resolved, is_partial=False)
    monkeypatch.setattr(
        "app.services.cierre_service._resolve_day_only_period",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._find_overlapping_period_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service.check_plan_quota_period",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._fetch_open_shift_for_window",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._compute_preview",
        AsyncMock(return_value={
            "totalSales": 0, "itemsSold": 0,
            "totalTips": 0, "totalTipTax": 0, "cashTips": 0,
            "totalCash": 0, "totalCard": 0, "totalDigital": 0, "totalCredit": 0,
            "gastosEfectivo": 0, "cashPurchases": 0, "cashExpected": 0,
            "openTablesCount": 0,
        }),
    )

    with pytest.raises(APIError) as excinfo:
        await create_cierre(_fake_request(tenant_id), body)

    assert excinfo.value.status_code == 409, (
        f"Expected 409 for duplicate period, got {excinfo.value.status_code}"
    )
    msg = str(excinfo.value)
    assert "Ya existe un cierre" in msg, f"Unexpected message: {msg}"
    assert not fake_conn.inserted, "Connection never reached the success path"


@pytest.mark.asyncio
async def test_create_cierre_other_unique_violation_is_409(monkeypatch):
    """Unique violations on other constraints also map to 409 (not 500)
    so the client always sees a sane status code."""
    tenant_id = uuid4()
    session = SimpleNamespace(tenant_id=tenant_id, user_id=uuid4())

    fake_conn = _FakeConn(raise_on_insert=_unique_violation("some_other_uq"))

    @asynccontextmanager
    async def fake_db_conn(use_transaction: bool = True):
        yield fake_conn

    monkeypatch.setattr(
        "app.services.cierre_service.get_db_connection",
        fake_db_conn,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.require_valid_session",
        lambda req: session,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_tenant_timezone",
        AsyncMock(return_value="America/Bogota"),
    )
    resolved = SimpleNamespace(
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        period_start_time=datetime(2026, 8, 23, 17, 10, tzinfo=BOG),
        period_end_time=datetime(2026, 8, 23, 22, 30, tzinfo=BOG),
        shift_template_id=uuid4(),
    )
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_cierre_period_fields",
        AsyncMock(return_value=resolved),
    )
    window = SimpleNamespace(resolved=resolved, is_partial=False)
    monkeypatch.setattr(
        "app.services.cierre_service._resolve_day_only_period",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._find_overlapping_period_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service.check_plan_quota_period",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._fetch_open_shift_for_window",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._compute_preview",
        AsyncMock(return_value={
            "totalSales": 0, "itemsSold": 0,
            "totalTips": 0, "totalTipTax": 0, "cashTips": 0,
            "totalCash": 0, "totalCard": 0, "totalDigital": 0, "totalCredit": 0,
            "gastosEfectivo": 0, "cashPurchases": 0, "cashExpected": 0,
            "openTablesCount": 0,
        }),
    )

    body = CierreCreate(
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        shift_template_id=uuid4(),
        cash_counted=0.0,
        payment_breakdown_reported=None,
    )

    with pytest.raises(APIError) as excinfo:
        await create_cierre(_fake_request(tenant_id), body)

    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_create_cierre_happy_path_distinct_shifts_succeed(monkeypatch):
    """Regression for #898: two distinct shift_template_id values on the same
    tenant+day must BOTH succeed (the original bug blocked the second INSERT).
    """
    tenant_id = uuid4()
    session = SimpleNamespace(tenant_id=tenant_id, user_id=uuid4())

    shift_a = uuid4()
    shift_b = uuid4()

    fake_conn = _FakeConn()  # INSERT succeeds

    @asynccontextmanager
    async def fake_db_conn(use_transaction: bool = True):
        yield fake_conn

    monkeypatch.setattr(
        "app.services.cierre_service.get_db_connection",
        fake_db_conn,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.require_valid_session",
        lambda req: session,
    )
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_tenant_timezone",
        AsyncMock(return_value="America/Bogota"),
    )

    async def _stub_resolve(*args, **kwargs):
        stid = kwargs.get("shift_template_id") or (args[2] if len(args) > 2 else None)
        return SimpleNamespace(
            period_start=date(2026, 8, 23),
            period_end=date(2026, 8, 23),
            period_start_time=datetime(2026, 8, 23, 7, 0, tzinfo=BOG),
            period_end_time=datetime(2026, 8, 23, 17, 0, tzinfo=BOG),
            shift_template_id=stid,
        )
    monkeypatch.setattr(
        "app.services.cierre_service.resolve_cierre_period_fields",
        _stub_resolve,
    )
    monkeypatch.setattr(
        "app.services.cierre_service._resolve_day_only_period",
        AsyncMock(side_effect=lambda conn, tid, resolved, **kw: SimpleNamespace(
            resolved=resolved, is_partial=False,
        )),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._find_overlapping_period_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service.check_plan_quota_period",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._fetch_open_shift_for_window",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._compute_preview",
        AsyncMock(return_value={
            "totalSales": 0, "itemsSold": 0,
            "totalTips": 0, "totalTipTax": 0, "cashTips": 0,
            "totalCash": 0, "totalCard": 0, "totalDigital": 0, "totalCredit": 0,
            "gastosEfectivo": 0, "cashPurchases": 0, "cashExpected": 0,
            "openTablesCount": 0,
        }),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._compute_breakdown_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._compute_method_outflow_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.cierre_service._merge_breakdown_with_outflows",
        lambda breakdown, outflows: [],
    )

    body_a = CierreCreate(
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        shift_template_id=shift_a,
        cash_counted=1000.0,
        payment_breakdown_reported=None,
    )
    result_a = await create_cierre(_fake_request(tenant_id), body_a)
    assert result_a["success"] is True

    body_b = CierreCreate(
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        shift_template_id=shift_b,
        cash_counted=500.0,
        payment_breakdown_reported=None,
    )
    result_b = await create_cierre(_fake_request(tenant_id), body_b)
    assert result_b["success"] is True

    assert fake_conn.inserted, "Both inserts should have reached the DB"
