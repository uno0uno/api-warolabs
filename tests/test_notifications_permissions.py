"""Permission smoke tests for notifications endpoints."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.notifications import router as notifications_router


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


def test_owner_role_passes_notifications_under_enforce():
    """Owner reaches GET /notifications under enforce."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(notifications_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.notifications.notifications_service.get_unread_notifications",
             new=AsyncMock(return_value={"success": True, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/notifications")

    assert response.status_code == 200


def test_kitchen_role_denied_notifications_under_enforce():
    """Kitchen role hits 403 on POS-scoped notifications."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(notifications_router)

    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/notifications/stream")

    assert response.status_code == 403
    assert "pos" in response.json()["detail"].lower()


def test_cashier_role_passes_mark_all_notifications_under_enforce():
    """Cashier keeps POS notification controls under enforce."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(notifications_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.notifications.notifications_service.mark_all_notifications_read",
             new=AsyncMock(return_value={"success": True, "data": {"marked_read": 0}}),
         ):
        client = TestClient(app)
        response = client.post("/notifications/read-all")

    assert response.status_code == 200

