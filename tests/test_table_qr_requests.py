"""Tests for Table QR request accept/reject (api-warolabs#268)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import APIError
from app.core.middleware import SessionContext
from app.routers import table_qr_requests as table_qr_requests_router
from app.services import table_qr_requests_service


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
         patch("app.services.table_qr_requests_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await table_qr_requests_service.reject_request(MagicMock(), request_id)
        assert result["data"]["status"] == "rejected"


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
