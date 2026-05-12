"""End-to-end smoke tests for OPERACIONES group endpoints (stations.py).

Sub-task E2.7 of Epic 2 (#191). Validates that:
1. Owner role under enforce reaches a stations handler (matrix grants OPERACIONES).
2. Kitchen role under enforce gets 403 on stations (kitchen lacks OPERACIONES).
3. The public KDS read `GET /api/stations/{id}` does NOT carry the gate —
   the tablet hits it without a session and must reach the handler.

Pairs with `tests/test_pos_permissions.py` (#188 KDS-passthrough pattern).
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.stations import router as stations_router


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role):
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": role,
    })


def _enforce_db_ctx():
    @asynccontextmanager
    async def _ctx():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetch = AsyncMock(return_value=[])
        yield conn
    return _ctx


# ─── Tests ────────────────────────────────────────────────────────────


def test_owner_role_passes_operaciones_endpoint_under_enforce():
    """Owner reaches GET /api/stations under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(stations_router, prefix="/api/stations")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.stations.stations_service.list_stations",
             new=AsyncMock(return_value={"success": True, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/api/stations")

    assert response.status_code == 200


def test_kitchen_role_denied_operaciones_endpoint_under_enforce():
    """Kitchen role hits 403 on stations — kitchen lacks OPERACIONES."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(stations_router, prefix="/api/stations")

    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/api/stations")

    assert response.status_code == 403
    assert "operaciones" in response.json()["detail"].lower()


def test_kds_public_station_read_bypasses_gate():
    """`GET /api/stations/{id}` is excluded — KDS tablet calls without session."""
    app = FastAPI()
    app.include_router(stations_router, prefix="/api/stations")

    station_id = uuid4()

    @asynccontextmanager
    async def _public_db_ctx(use_transaction=False):
        conn = MagicMock()
        # First fetchrow: station row; second fetchval: kds_enabled boolean.
        conn.fetchrow = AsyncMock(return_value={
            "id": station_id,
            "name": "Cocina",
            "kitchen_name": "Pizza",
            "color": "#FF0000",
            "is_active": True,
            "alert_threshold_1_min": 8,
            "alert_threshold_2_min": 15,
            "tenant_id": uuid4(),
        })
        conn.fetchval = AsyncMock(return_value=True)
        yield conn

    # No session context, no require_valid_session — the endpoint takes none.
    with patch("app.routers.stations.get_db_connection", side_effect=_public_db_ctx):
        client = TestClient(app)
        response = client.get(f"/api/stations/{station_id}")

    # 200 proves the gate didn't trip — KDS tablet can read without auth.
    assert response.status_code == 200
    assert response.json()["data"]["kds_enabled"] is True
