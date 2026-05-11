"""End-to-end smoke tests for INTEGRACIONES group endpoints under require_module(INTEGRACIONES).

Sub-task E2.13 of Epic 2 (#197). Validates that:
1. Owner role under enforce reaches a gated session-authenticated endpoint.
2. Cashier role under enforce gets 403 on the gated session endpoint (cashier
   lacks INTEGRACIONES — admin + owner only by matrix).
3. **API-key bypass test**: a request with NO SessionContext (mimicking an
   API-key call) successfully reaches a /v1/* handler under enforce. Proves
   the `require_module` early-return behavior protects API-key callers.

Scope: 3 files, 48 endpoints total — api_tokens.py (6, session), public_api.py
(25, api_key), v1_ordering.py (17 across 5 sub-routers, api_key).

The 3rd test documents the architecture: API-key middleware sets
`request.state.tenant_context` only, NEVER `request.state.session_context`.
`get_session_context()` returns an empty `SessionContext()` with `is_valid=False`,
which `require_module()` short-circuits on (permissions.py:339-343). So gating
all 48 endpoints is safe — api-key requests pass via the early-return.

Pairs with `tests/test_equipo_permissions.py` (#196 reference impl).
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
    """API-key request (no SessionContext) reaches /v1/cart handler under enforce.

    Mimics the production API-key flow: the api-key middleware sets
    `request.state.tenant_context` only — never `request.state.session_context`.
    `get_session_context()` therefore returns an empty `SessionContext()` with
    `is_valid=False`, and `require_module()` short-circuits via the early-return
    documented in permissions.py:339-343.

    This test guards against accidental future changes to the early-return —
    if anyone tightens the gate to deny invalid sessions instead of bypassing,
    every API-key caller in production would 403. This test catches it before
    merge.
    """
    invalid_session = SessionContext()  # empty / is_valid=False — mimics no-session API-key request
    app = FastAPI()
    app.include_router(v1_cart_router)

    with patch("app.core.middleware.get_session_context", return_value=invalid_session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.v1_ordering.validate_api_key_auth",
             return_value=(str(uuid4()), {"scopes": ["read"]}),
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

    # No 403 — handler reached because the gate early-returns on invalid sessions.
    # If this asserts != 200, the early-return behavior is broken and every
    # API-key caller in prod would 403.
    assert response.status_code == 200
