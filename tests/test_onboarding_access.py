from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.middleware import SessionContext, session_validation_middleware
from app.core.onboarding_access import is_pending_session_route_allowed


def _request(method: str, path: str) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 443),
    })


def _pending_session() -> dict:
    return {
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "new@example.com",
        "name": None,
        "expires_at": None,
        "is_active": True,
        "role": None,
        "lifecycle_status": "pending",
        "onboarding_state": "business_profile_pending",
        "next_step": "business_profile",
    }


def test_pending_allowlist_is_exact():
    assert is_pending_session_route_allowed("GET", "/auth/session")
    assert is_pending_session_route_allowed("GET", "/legal/terms/current")
    assert is_pending_session_route_allowed("GET", "/onboarding/financial-profile")
    assert is_pending_session_route_allowed("PUT", "/onboarding/financial-profile")
    assert is_pending_session_route_allowed("POST", "/billing/subscribe")
    assert not is_pending_session_route_allowed("GET", "/legal/terms/audit")
    assert not is_pending_session_route_allowed("DELETE", "/billing/subscription")
    assert not is_pending_session_route_allowed("GET", "/orders")


def test_session_context_exposes_server_owned_onboarding_state():
    session = SessionContext(_pending_session())
    assert session.lifecycle_status == "pending"
    assert session.onboarding_state == "business_profile_pending"
    assert session.next_step == "business_profile"
    assert session.role is None


@pytest.mark.asyncio
async def test_pending_session_is_denied_before_operational_handler():
    request = _request("GET", "/orders")
    call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

    with patch(
        "app.core.security.get_session_from_request",
        new=AsyncMock(return_value=_pending_session()),
    ):
        response = await session_validation_middleware(request, call_next)

    assert response.status_code == 403
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_session_can_reach_onboarding_status():
    request = _request("GET", "/onboarding/status")
    call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

    with patch(
        "app.core.security.get_session_from_request",
        new=AsyncMock(return_value=_pending_session()),
    ):
        response = await session_validation_middleware(request, call_next)

    assert response.status_code == 200
    call_next.assert_awaited_once()
