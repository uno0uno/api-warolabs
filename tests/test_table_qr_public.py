"""Tests for public Table QR menu and submit (api-warolabs#267)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import public_table_qr as public_table_qr_router
from app.services import public_table_qr_service


def _active_ctx():
    return {
        "tenant_id": uuid4(),
        "table_id": uuid4(),
        "tenant_slug": "cafe-demo",
        "display_name": "Café Demo",
        "table_name": "Mesa 1",
        "is_currently_open": True,
    }


@pytest.mark.asyncio
async def test_get_menu_for_token_returns_table_qr_products():
    ctx = _active_ctx()
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": uuid4(), "name": "Bebidas", "description": None}],
        [{
            "id": uuid4(),
            "name": "Café",
            "description": None,
            "price": 5000,
            "image_url": None,
            "category_id": uuid4(),
            "category_name": "Bebidas",
            "is_available": True,
            "preparation_time": 5,
            "allow_modifiers": False,
            "has_modifiers": False,
        }],
    ])

    with patch(
        "app.services.public_table_qr_service.resolve_table_qr_context",
        new_callable=AsyncMock,
        return_value=ctx,
    ), patch("app.services.public_table_qr_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await public_table_qr_service.get_menu_for_token("tok-abc")
        assert result["restaurant_name"] == "Café Demo"
        assert result["table_name"] == "Mesa 1"
        assert result["is_currently_open"] is True
        assert len(result["products"]) == 1
        assert result["products"][0]["price"] == 5000.0


@pytest.mark.asyncio
async def test_get_menu_inactive_token_404():
    with patch(
        "app.services.public_table_qr_service.resolve_table_qr_context",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc:
            await public_table_qr_service.get_menu_for_token("bad")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_submit_requires_open_restaurant():
    ctx = _active_ctx()
    ctx["is_currently_open"] = False

    with patch(
        "app.services.public_table_qr_service.resolve_table_qr_context",
        new_callable=AsyncMock,
        return_value=ctx,
    ):
        with pytest.raises(HTTPException) as exc:
            await public_table_qr_service.submit_table_qr_request(
                MagicMock(client=MagicMock(host="127.0.0.1")),
                "tok",
                items=[{"product_id": str(uuid4()), "quantity": 1, "modifiers": []}],
                payment_method="cash",
                payment_method_id=None,
                customer_notes=None,
            )
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_submit_duplicate_pending_request_returns_existing_id():
    ctx = _active_ctx()
    request_id = uuid4()
    snapshots = [{
        "product_id": str(uuid4()),
        "quantity": 1,
        "unit_price": 10000.0,
        "modifiers": [],
        "notes": None,
        "line_total": 10000.0,
    }]
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": request_id, "created_at": None})
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    with patch(
        "app.services.public_table_qr_service.resolve_table_qr_context",
        new_callable=AsyncMock,
        return_value=ctx,
    ), patch(
        "app.services.public_table_qr_service.get_db_connection",
    ) as mock_conn, patch(
        "app.services.public_table_qr_service._build_item_snapshots",
        new=AsyncMock(return_value=(snapshots, 10000)),
    ), patch(
        "app.services.public_table_qr_service._validate_payment_selection",
        new=AsyncMock(),
    ), patch(
        "app.services.public_table_qr_service.notifications_service.create_table_qr_notification",
        new=AsyncMock(),
    ) as notify:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await public_table_qr_service.submit_table_qr_request(
            MagicMock(client=MagicMock(host="127.0.0.1")),
            "tok",
            items=[{"product_id": snapshots[0]["product_id"], "quantity": 1, "modifiers": []}],
            payment_method="cash",
            payment_method_id=None,
            customer_notes=None,
        )

    assert result["request_id"] == str(request_id)
    assert result["status"] == "pending"
    assert conn.fetchrow.await_count == 1
    notify.assert_not_awaited()


def test_menu_endpoint_success():
    app = FastAPI()
    app.include_router(public_table_qr_router.router, prefix="/public/table-qr")

    menu_payload = {
        "restaurant_name": "Café",
        "table_name": "Mesa 1",
        "is_currently_open": True,
        "categories": [],
        "products": [],
    }

    with patch(
        "app.routers.public_table_qr.public_table_qr_service.get_menu_for_token",
        new_callable=AsyncMock,
        return_value=menu_payload,
    ):
        client = TestClient(app)
        res = client.get("/public/table-qr/tok-abc/menu")
        assert res.status_code == 200
        assert res.json()["success"] is True
        assert res.json()["data"]["table_name"] == "Mesa 1"


def test_submit_endpoint_delegates_to_service():
    app = FastAPI()
    app.include_router(public_table_qr_router.router, prefix="/public/table-qr")

    modifier_id = uuid4()
    submit_result = {
        "request_id": str(uuid4()),
        "status": "pending",
        "table_name": "Mesa 1",
        "total_amount": 10000.0,
        "message": "Pedido recibido — el restaurante lo confirmará.",
    }

    with patch(
        "app.routers.public_table_qr.public_table_qr_service.submit_table_qr_request",
        new_callable=AsyncMock,
        return_value=submit_result,
    ):
        client = TestClient(app)
        product_id = str(uuid4())
        res = client.post(
            f"/public/table-qr/tok-abc/requests",
            json={
                "items": [{
                    "product_id": product_id,
                    "quantity": 1,
                    "modifiers": [{"id": str(modifier_id), "quantity": 2}],
                }],
                "payment_method": "cash",
            },
        )
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "pending"

        forwarded_items = (
            public_table_qr_service.submit_table_qr_request.await_args.kwargs["items"]
        )
        assert forwarded_items[0]["modifiers"] == [{
            "id": str(modifier_id),
            "quantity": 2,
        }]


def test_submit_endpoint_defaults_modifier_quantity_to_one():
    app = FastAPI()
    app.include_router(public_table_qr_router.router, prefix="/public/table-qr")

    with patch(
        "app.routers.public_table_qr.public_table_qr_service.submit_table_qr_request",
        new_callable=AsyncMock,
        return_value={"status": "pending"},
    ):
        client = TestClient(app)
        product_id = str(uuid4())
        modifier_id = uuid4()
        res = client.post(
            "/public/table-qr/tok-abc/requests",
            json={
                "items": [{
                    "product_id": product_id,
                    "quantity": 1,
                    "modifiers": [{"id": str(modifier_id)}],
                }],
            },
        )

        assert res.status_code == 200
        forwarded_items = (
            public_table_qr_service.submit_table_qr_request.await_args.kwargs["items"]
        )
        assert forwarded_items[0]["modifiers"][0]["quantity"] == 1


def test_submit_endpoint_rejects_invalid_modifier_quantity():
    app = FastAPI()
    app.include_router(public_table_qr_router.router, prefix="/public/table-qr")

    client = TestClient(app)
    res = client.post(
        "/public/table-qr/tok-abc/requests",
        json={
            "items": [{
                "product_id": str(uuid4()),
                "quantity": 1,
                "modifiers": [{"id": str(uuid4()), "quantity": 0}],
            }],
        },
    )

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_build_item_snapshots_multiplies_and_persists_modifier_quantity():
    tenant_id = uuid4()
    product_id = uuid4()
    modifier_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": product_id, "name": "Hamburguesa", "price": 10000}],
        [{"id": modifier_id, "name": "Tocineta", "price": 2500}],
    ])

    with patch(
        "app.services.public_table_qr_service.validate_products_belong_to_tenant",
        new=AsyncMock(),
    ), patch(
        "app.services.public_table_qr_service.validate_modifiers_for_item",
        new=AsyncMock(),
    ):
        snapshots, total = await public_table_qr_service._build_item_snapshots(
            conn,
            tenant_id,
            [{
                "product_id": str(product_id),
                "quantity": 2,
                "modifiers": [{"id": str(modifier_id), "quantity": 3}],
            }],
        )

    assert total == 35000
    assert snapshots[0]["line_total"] == 35000.0
    assert snapshots[0]["modifiers"] == [{
        "id": str(modifier_id),
        "name": "Tocineta",
        "price": 2500.0,
        "quantity": 3,
    }]


@pytest.mark.asyncio
async def test_build_item_snapshots_rejects_invalid_modifier_quantity():
    tenant_id = uuid4()
    product_id = uuid4()
    modifier_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": product_id, "name": "Hamburguesa", "price": 10000}],
        [{"id": modifier_id, "name": "Tocineta", "price": 2500}],
    ])

    with patch(
        "app.services.public_table_qr_service.validate_products_belong_to_tenant",
        new=AsyncMock(),
    ), patch(
        "app.services.public_table_qr_service.validate_modifiers_for_item",
        new=AsyncMock(),
    ):
        with pytest.raises(HTTPException) as exc:
            await public_table_qr_service._build_item_snapshots(
                conn,
                tenant_id,
                [{
                    "product_id": str(product_id),
                    "quantity": 1,
                    "modifiers": [{"id": str(modifier_id), "quantity": 0}],
                }],
            )

    assert exc.value.status_code == 400
