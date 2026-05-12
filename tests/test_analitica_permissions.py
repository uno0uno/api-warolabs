"""End-to-end smoke tests for ANALITICA group endpoints under require_module(ANALITICA).

Sub-task E2.9 of Epic 2 (#193). Validates that:
1. Owner role under enforce reaches an analytics handler.
2. Kitchen role under enforce gets 403 (kitchen lacks ANALITICA — default matrix
   only grants it to OWNER, ADMIN, SUPERVISOR).

`articles.py` is NOT covered here — it's the public blog (no session required),
explicitly out of scope for this PR (audit doc classifies it as `public`).

Pairs with `tests/test_menu_permissions.py` (#190 reference impl).
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
from app.routers.analytics import router as analytics_router


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


def test_owner_role_passes_analitica_endpoint_under_enforce():
    """Owner reaches GET /analytics/menu-analysis under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(analytics_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.analytics.analytics_service.get_menu_analysis",
             new=AsyncMock(return_value={"data": []}),
         ):
        client = TestClient(app)
        response = client.get("/analytics/menu-analysis")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_kitchen_role_denied_analitica_endpoint_under_enforce():
    """Kitchen role hits 403 on ANALITICA — kitchen lacks ANALITICA in default matrix."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(analytics_router)

    # Stub get_role_modules to return kitchen's default set (DESPACHO only).
    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/analytics/menu-analysis")

    assert response.status_code == 403
    assert "analitica" in response.json()["detail"].lower()
