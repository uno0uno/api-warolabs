"""End-to-end smoke tests for INTEGRACIONES group endpoints under require_module(INTEGRACIONES).

Sub-task E2.13 of Epic 2 (#197). Validates that:
1. Owner role under enforce reaches a gated session-authenticated endpoint.
2. Cashier role under enforce gets 403 on the gated session endpoint (cashier
   lacks INTEGRACIONES — admin + owner only by matrix).
3. **API-key bypass test**: a request with a valid ApiKeyContext (and the
   role-less pseudo-session middleware builds for API keys) reaches a /v1/*
   handler under enforce. Proves require_module bypasses staff gates for
   API-key callers (#819).

Scope: 3 files, 48 endpoints total — api_tokens.py (6, session), public_api.py
(25, api_key), v1_ordering.py (17 across 5 sub-routers, api_key).

Pairs with `tests/test_equipo_permissions.py` (#196 reference impl).
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import ApiKeyContext, SessionContext
from app.core.permissions import Module
from app.routers.api_tokens import router as api_tokens_router
from app.routers.v1_ordering import router as v1_cart_router


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
        # get_effective_plan_slug awaits fetchrow; None → non-starter path
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn
    return _ctx


# ─── Tests ────────────────────────────────────────────────────────────


def test_owner_role_passes_integraciones_endpoint_under_enforce():
    """Owner reaches GET /api-tokens under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(api_tokens_router, prefix="/api-tokens")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.api_tokens.list_api_tokens",
             new=AsyncMock(return_value={"data": [], "total": 0}),
         ):
        client = TestClient(app)
        response = client.get("/api-tokens")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_cashier_role_denied_integraciones_endpoint_under_enforce():
    """Cashier role hits 403 on INTEGRACIONES — cashier lacks INTEGRACIONES in default matrix."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(api_tokens_router, prefix="/api-tokens")

    # Stub get_role_modules to return cashier's default set (POS + VENTAS only).
    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/api-tokens")

    assert response.status_code == 403
    assert "integraciones" in response.json()["detail"].lower()


def test_api_key_request_bypasses_gate_under_enforce():
    """Valid API-key context bypasses staff INTEGRACIONES gate under enforce.

    Matches production middleware: API keys get a role-less pseudo-session
    (`is_valid=True`, `role=None`) plus `ApiKeyContext(is_valid=True)`.
    Without the API-key early-return, require_module would deny no-membership
    and every /v1/* caller would 403 on enforce tenants (#819).
    """
    tenant_id = uuid4()
    pseudo_session = SessionContext({
        "user_id": None,
        "tenant_id": tenant_id,
        "email": None,
        "name": "API Key (waro_sk_test...)",
        "expires_at": None,
        "is_active": True,
        # role intentionally omitted — middleware does not plumb staff roles
    })
    api_key_ctx = ApiKeyContext({
        "token_id": str(uuid4()),
        "tenant_id": str(tenant_id),
        "scopes": ["read", "write"],
    })
    app = FastAPI()
    app.include_router(v1_cart_router)

    with patch("app.core.middleware.get_session_context", return_value=pseudo_session), \
         patch("app.core.middleware.get_api_key_context", return_value=api_key_ctx), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.v1_ordering.validate_api_key_auth",
             return_value=(str(tenant_id), {"scopes": ["read"]}),
         ), \
         patch(
             "app.routers.v1_ordering.online_cart_service.create_cart_with_batch_items",
             new=AsyncMock(return_value={"success": True, "data": {"session_id": "test"}}),
         ):
        client = TestClient(app)
        response = client.post(
            "/v1/cart/batch",
            json={"items": [], "order_type": "delivery"},
        )

    assert response.status_code == 200
