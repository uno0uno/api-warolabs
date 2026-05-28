"""Tests for Table QR token lifecycle (api-warolabs#266)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import APIError
from app.core.middleware import SessionContext
from app.routers import public_table_qr as public_table_qr_router
from app.routers import tables as tables_router
from app.services import public_table_qr_service, tables_service


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "op@example.com",
        "name": "Operator",
        "expires_at": None,
        "is_active": True,
        "role": "supervisor",
    })


@pytest.mark.asyncio
async def test_resolve_table_qr_token_active():
    row = {
        "tenant_id": uuid4(),
        "table_id": uuid4(),
        "table_name": "Mesa 1",
        "qr_enabled": True,
        "table_active": True,
        "tenant_slug": "cafe-demo",
        "display_name": "Café Demo",
        "table_qr_module_enabled": True,
        "profile_active": True,
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)

    row["business_hours"] = None
    row["is_manually_open"] = True

    with patch("app.services.public_table_qr_service.get_db_connection") as mock_get_conn, \
         patch("app.services.public_table_qr_service.is_currently_open", return_value=True):
        mock_get_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_get_conn.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await public_table_qr_service.resolve_table_qr_token("tok-abc")
        assert result["tenant_slug"] == "cafe-demo"
        assert result["table_name"] == "Mesa 1"


@pytest.mark.asyncio
async def test_resolve_table_qr_token_disabled_module_returns_none():
    row = {
        "table_name": "Mesa 1",
        "qr_enabled": True,
        "table_active": True,
        "tenant_slug": "cafe-demo",
        "display_name": "Café Demo",
        "table_qr_module_enabled": False,
        "profile_active": True,
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)

    row["business_hours"] = None
    row["is_manually_open"] = True

    with patch("app.services.public_table_qr_service.get_db_connection") as mock_get_conn, \
         patch("app.services.public_table_qr_service.is_currently_open", return_value=True):
        mock_get_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_get_conn.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await public_table_qr_service.resolve_table_qr_token("tok-abc") is None


def test_public_resolve_endpoint_404_when_inactive():
    app = FastAPI()
    app.include_router(public_table_qr_router.router, prefix="/public/table-qr")

    with patch(
        "app.routers.public_table_qr.public_table_qr_service.resolve_table_qr_token",
        new_callable=AsyncMock,
        return_value=None,
    ):
        client = TestClient(app)
        res = client.get("/public/table-qr/unknown-token")
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_regenerate_table_qr_rejects_bar():
    session = _session()
    table_id = uuid4()

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={
        "id": table_id,
        "name": "Barra",
        "is_bar": True,
        "is_active": True,
        "deleted_at": None,
        "qr_enabled": False,
        "qr_public_token": None,
    })

    with patch("app.services.tables_service.require_valid_session", return_value=session), \
         patch("app.services.tables_service.get_db_connection") as mock_conn:
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(APIError) as exc:
            await tables_service.regenerate_table_qr_token(MagicMock(), table_id)
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_set_table_qr_enabled_generates_token():
    session = _session()
    table_id = uuid4()
    token = "generated-token-xyz"

    updated_row = {
        "id": table_id,
        "name": "Mesa 2",
        "capacity": 4,
        "status": "free",
        "is_active": True,
        "is_bar": False,
        "qr_enabled": True,
        "qr_public_token": token,
        "created_at": MagicMock(isoformat=lambda: "2026-01-01T00:00:00"),
    }

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": table_id,
            "name": "Mesa 2",
            "is_bar": False,
            "is_active": True,
            "deleted_at": None,
            "qr_enabled": False,
            "qr_public_token": None,
        },
        updated_row,
    ])

    with patch("app.services.tables_service.require_valid_session", return_value=session), \
         patch("app.services.tables_service.get_db_connection") as mock_conn, \
         patch("app.services.tables_service._generate_unique_qr_token", new_callable=AsyncMock, return_value=token):
        mock_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_conn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await tables_service.set_table_qr_enabled(MagicMock(), table_id, True)
        assert result["data"]["qr_public_token"] == token
        assert result["data"]["qr_enabled"] is True


def test_patch_table_qr_route_delegates_to_service():
    """HTTP layer: PATCH /tables/{id}/qr is registered (warocol.com#976)."""
    app = FastAPI()
    app.include_router(tables_router.router, prefix="/tables")
    table_id = uuid4()
    payload = {
        "success": True,
        "data": {
            "id": str(table_id),
            "name": "Mesa 1",
            "qr_enabled": True,
            "qr_public_token": "tok-abc",
        },
    }

    with patch(
        "app.routers.tables.tables_service.set_table_qr_enabled",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        client = TestClient(app)
        res = client.patch(f"/tables/{table_id}/qr", json={"enabled": True})

    assert res.status_code == 200
    assert res.json()["data"]["qr_enabled"] is True


def test_post_table_qr_token_regenerate_route_delegates_to_service():
    """HTTP layer: POST /tables/{id}/qr-token/regenerate is registered (warocol.com#976)."""
    app = FastAPI()
    app.include_router(tables_router.router, prefix="/tables")
    table_id = uuid4()
    payload = {
        "success": True,
        "data": {
            "id": str(table_id),
            "name": "Mesa 1",
            "qr_enabled": True,
            "qr_public_token": "tok-new",
        },
    }

    with patch(
        "app.routers.tables.tables_service.regenerate_table_qr_token",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        client = TestClient(app)
        res = client.post(f"/tables/{table_id}/qr-token/regenerate")

    assert res.status_code == 200
    assert res.json()["data"]["qr_public_token"] == "tok-new"
