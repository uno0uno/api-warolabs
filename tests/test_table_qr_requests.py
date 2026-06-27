"""Tests for Table QR request accept/reject (api-warolabs#268) and detail GET (#275)."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import APIError
from app.core.middleware import SessionContext
from app.routers import table_qr_requests as table_qr_requests_router
from app.services import table_qr_requests_service


def _pending_request_row(**overrides):
    product_id = uuid4()
    base = {
        "id": uuid4(),
        "table_id": uuid4(),
        "status": "pending",
        "items": json.dumps([
            {
                "product_id": str(product_id),
                "quantity": 2,
                "unit_price": 10.0,
                "modifiers": [],
                "notes": None,
                "line_total": 20.0,
            },
        ]),
        "payment_method": "cash",
        "payment_method_id": None,
        "customer_notes": "Sin cebolla",
        "created_at": datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        "table_name": "Mesa 1",
    }
    base.update(overrides)
    return base


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "despacho@example.com",
        "name": "Despacho",
        "expires_at": None,
        "is_active": True,
        "role": "supervisor",
    })


@pytest.mark.asyncio
async def test_reject_request_marks_rejected():
    session = _session()
    request_id = uuid4()
    table_id = uuid4()

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": request_id, "table_id": table_id})

    with patch("app.services.table_qr_requests_service.require_valid_session", return_value=session), \
         patch("app.services.table_qr_requests_service.get_db_connection") as mock_conn, \
         patch(
             "app.services.table_qr_requests_service.notifications_service.mark_table_qr_notifications_read",
             new_callable=AsyncMock,
         ) as mark_read_mock:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await table_qr_requests_service.reject_request(MagicMock(), request_id)
        assert result["data"]["status"] == "rejected"
        mark_read_mock.assert_awaited_once_with(conn, session.tenant_id, request_id)


@pytest.mark.asyncio
async def test_accept_conflicting_tables_409():
    session = _session()
    id1, id2 = uuid4(), uuid4()
    table_a, table_b = uuid4(), uuid4()

    rows = [
        {
            "id": id1,
            "table_id": table_a,
            "status": "pending",
            "items": json.dumps([{"product_id": str(uuid4()), "quantity": 1, "unit_price": 10.0}]),
            "payment_method": "cash",
            "payment_method_id": None,
            "customer_notes": None,
            "created_at": MagicMock(),
        },
        {
            "id": id2,
            "table_id": table_b,
            "status": "pending",
            "items": json.dumps([{"product_id": str(uuid4()), "quantity": 1, "unit_price": 5.0}]),
            "payment_method": "cash",
            "payment_method_id": None,
            "customer_notes": None,
            "created_at": MagicMock(),
        },
    ]

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    conn.fetch = AsyncMock(return_value=rows)

    with patch("app.services.table_qr_requests_service.require_valid_session", return_value=session), \
         patch("app.services.table_qr_requests_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(APIError) as exc:
            await table_qr_requests_service.accept_requests(MagicMock(), [id1, id2])
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_accept_conflicting_payment_methods_409():
    session = _session()
    id1, id2 = uuid4(), uuid4()
    table_id = uuid4()

    rows = [
        {
            "id": id1,
            "table_id": table_id,
            "status": "pending",
            "items": "[]",
            "payment_method": "cash",
            "payment_method_id": None,
            "customer_notes": None,
            "created_at": MagicMock(),
        },
        {
            "id": id2,
            "table_id": table_id,
            "status": "pending",
            "items": "[]",
            "payment_method": "card",
            "payment_method_id": None,
            "customer_notes": None,
            "created_at": MagicMock(),
        },
    ]

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    conn.fetch = AsyncMock(return_value=rows)

    with patch("app.services.table_qr_requests_service.require_valid_session", return_value=session), \
         patch("app.services.table_qr_requests_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(APIError) as exc:
            await table_qr_requests_service.accept_requests(MagicMock(), [id1, id2])
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_accept_requests_passes_persisted_modifier_quantity_to_tab_core():
    session = _session()
    request_id = uuid4()
    table_id = uuid4()
    request_items = [{
        "product_id": str(uuid4()),
        "quantity": 1,
        "unit_price": 10.0,
        "modifiers": [{
            "id": str(uuid4()),
            "name": "Tocineta",
            "price": 2.5,
            "quantity": 3,
        }],
        "line_total": 17.5,
    }]

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    conn.fetch = AsyncMock(side_effect=[
        [{
            "id": request_id,
            "table_id": table_id,
            "status": "pending",
            "items": json.dumps(request_items),
            "payment_method": None,
            "payment_method_id": None,
            "customer_notes": None,
            "created_at": MagicMock(),
        }],
        [{"id": request_id}],
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.table_qr_requests_service.require_valid_session",
        return_value=session,
    ), patch(
        "app.services.table_qr_requests_service.get_db_connection",
    ) as mock_conn, patch(
        "app.services.table_qr_requests_service._resolve_tenant_member_id",
        new=AsyncMock(return_value=uuid4()),
    ), patch(
        "app.services.table_qr_requests_service._ensure_open_session_in_tx",
        new=AsyncMock(return_value=uuid4()),
    ), patch(
        "app.services.table_qr_requests_service._add_tab_items_core",
        new=AsyncMock(return_value={
            "session_id": uuid4(),
            "order_id": uuid4(),
            "order_number": 12,
            "items_count": 1,
            "total_amount": 17.5,
        }),
    ) as add_tab_mock, patch(
        "app.services.table_qr_requests_service.fire_table_items",
        new=AsyncMock(),
    ), patch(
        "app.services.table_qr_requests_service.notifications_service.mark_table_qr_notifications_read",
        new_callable=AsyncMock,
    ) as mark_read_mock:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await table_qr_requests_service.accept_requests(
            MagicMock(),
            [request_id],
        )

    assert result["success"] is True
    forwarded_items = add_tab_mock.await_args.args[4]
    assert forwarded_items[0]["modifiers"][0]["quantity"] == 3
    mark_read_mock.assert_awaited_once_with(conn, session.tenant_id, request_id)


def test_list_endpoint_delegates():
    app = FastAPI()
    app.include_router(table_qr_requests_router.router, prefix="/table-qr-requests")

    payload = {"success": True, "data": {"tables": [], "total_pending": 0}}

    with patch(
        "app.routers.table_qr_requests.table_qr_requests_service.list_pending_grouped",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        client = TestClient(app)
        res = client.get("/table-qr-requests?status=pending")
        assert res.status_code == 200
        assert res.json()["data"]["total_pending"] == 0


def test_format_request_row_payment_display_with_submethod():
    row = {
        "id": uuid4(),
        "table_id": uuid4(),
        "status": "pending",
        "items": "[]",
        "payment_method": "transferencia",
        "payment_method_id": uuid4(),
        "customer_notes": None,
        "created_at": datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        "table_name": "Mesa 1",
        "payment_method_group_name": "Transferencia",
        "payment_method_name": "Bancolombia Ahorros",
    }
    data = table_qr_requests_service._format_request_row(row, "Pacific/Kiritimati")
    assert data["payment_display"] == "Transferencia · Bancolombia Ahorros"
    assert data["payment_method_group_name"] == "Transferencia"
    assert data["payment_method_name"] == "Bancolombia Ahorros"
    assert data["tenant_timezone"] == "Pacific/Kiritimati"


@pytest.mark.asyncio
async def test_get_request_returns_enriched_pending():
    session = _session()
    request_id = uuid4()
    row = _pending_request_row(
        id=request_id,
        payment_method="cash",
        payment_method_group_name="Efectivo",
        payment_method_name=None,
    )
    product_id = json.loads(row["items"])[0]["product_id"]

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="Pacific/Kiritimati")
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[{"id": UUID(product_id), "name": "Hamburguesa"}])

    with patch("app.services.table_qr_requests_service.require_valid_session", return_value=session), \
         patch("app.services.table_qr_requests_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await table_qr_requests_service.get_request(MagicMock(), request_id)

    assert result["success"] is True
    data = result["data"]
    assert data["id"] == str(request_id)
    assert data["table_name"] == "Mesa 1"
    assert data["status"] == "pending"
    assert data["total_amount"] == 20.0
    assert data["items"][0]["product_name"] == "Hamburguesa"
    assert data["customer_notes"] == "Sin cebolla"
    assert data["payment_display"] == "Efectivo"
    assert data["tenant_timezone"] == "Pacific/Kiritimati"


@pytest.mark.asyncio
async def test_get_request_not_found_404():
    session = _session()
    request_id = uuid4()

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with patch("app.services.table_qr_requests_service.require_valid_session", return_value=session), \
         patch("app.services.table_qr_requests_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(APIError) as exc:
            await table_qr_requests_service.get_request(MagicMock(), request_id)
        assert exc.value.status_code == 404


def test_get_endpoint_delegates():
    app = FastAPI()
    app.include_router(table_qr_requests_router.router, prefix="/table-qr-requests")

    request_id = uuid4()
    payload = {
        "success": True,
        "data": {
            "id": str(request_id),
            "table_id": str(uuid4()),
            "table_name": "Mesa 2",
            "status": "pending",
            "items": [],
            "item_count": 0,
            "total_amount": 0.0,
            "payment_method": None,
            "payment_method_id": None,
            "customer_notes": None,
            "created_at": "2026-05-20T12:00:00+00:00",
        },
    }

    with patch(
        "app.routers.table_qr_requests.table_qr_requests_service.get_request",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        client = TestClient(app)
        res = client.get(f"/table-qr-requests/{request_id}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == str(request_id)
