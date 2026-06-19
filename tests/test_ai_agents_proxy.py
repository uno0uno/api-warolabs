import hashlib
import hmac
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers import ai_agents


BODY = b'{"message":"ventas de hoy"}'
TENANT_ID = uuid4()
PROFILE_ID = uuid4()
MEMBER_ID = uuid4()


@pytest.fixture(autouse=True)
def _clear_permission_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role="owner", tenant_id=TENANT_ID):
    return SessionContext({
        "user_id": PROFILE_ID,
        "tenant_id": tenant_id,
        "email": "owner@example.com",
        "name": "Owner",
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


class FakeUpstreamResponse:
    def __init__(self, status_code=200, chunks=None, body=b""):
        self.status_code = status_code
        self._chunks = chunks or []
        self._body = body
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return self._body

    async def aclose(self):
        self.closed = True


class FakeAsyncClient:
    instances = []
    response = FakeUpstreamResponse()

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.closed = False
        self.sent_request = None
        FakeAsyncClient.instances.append(self)

    def build_request(self, method, url, content=None, headers=None):
        return {
            "method": method,
            "url": url,
            "content": content,
            "headers": headers or {},
        }

    async def send(self, request, stream=False):
        self.sent_request = request
        self.sent_stream = stream
        return FakeAsyncClient.response

    async def aclose(self):
        self.closed = True


def _client():
    app = FastAPI()
    app.include_router(ai_agents.router)
    return TestClient(app)


def test_sign_internal_request_matches_agent_api_contract():
    with patch.object(ai_agents.settings, "agent_internal_signature_secret", "secret"):
        signature = ai_agents._sign_internal_request(
            method="POST",
            path="/internal/ai/sales/messages/stream",
            request_id="req-1",
            tenant_id="tenant-1",
            profile_id="profile-1",
            member_id=None,
            scopes="orders:read",
            body=BODY,
        )

    canonical = "\n".join([
        "POST",
        "/internal/ai/sales/messages/stream",
        "req-1",
        "tenant-1",
        "profile-1",
        "",
        "orders:read",
        hashlib.sha256(BODY).hexdigest(),
    ])
    expected = hmac.new(b"secret", canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    assert signature == expected


def test_sales_proxy_streams_frames_and_preserves_request_id():
    session = _build_session(role="owner")
    FakeAsyncClient.instances = []
    FakeAsyncClient.response = FakeUpstreamResponse(
        chunks=[b"event: token\ndata: uno\n\n", b"event: final\ndata: done\n\n"],
    )

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.ai_agents.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.routers.ai_agents._resolve_member_id", AsyncMock(return_value=str(MEMBER_ID))), \
         patch.object(ai_agents.settings, "agent_api_url", "http://agent-api:8100"), \
         patch.object(ai_agents.settings, "agent_internal_signature_secret", "secret"), \
         patch("app.routers.ai_agents.httpx.AsyncClient", FakeAsyncClient):
        response = _client().post(
            "/ai/sales/messages/stream",
            content=BODY,
            headers={
                "Content-Type": "application/json",
                "x-waro-request-id": "req-preserved",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-waro-request-id"] == "req-preserved"
    assert response.content == b"event: token\ndata: uno\n\nevent: final\ndata: done\n\n"

    sent = FakeAsyncClient.instances[0].sent_request
    assert sent["method"] == "POST"
    assert sent["url"] == "http://agent-api:8100/internal/ai/sales/messages/stream"
    assert sent["content"] == BODY
    assert sent["headers"]["x-waro-tenant-id"] == str(TENANT_ID)
    assert sent["headers"]["x-waro-profile-id"] == str(PROFILE_ID)
    assert sent["headers"]["x-waro-member-id"] == str(MEMBER_ID)
    assert sent["headers"]["x-waro-request-id"] == "req-preserved"
    assert sent["headers"]["x-waro-scopes"] == "orders:read"
    assert sent["headers"]["x-waro-internal-signature"]
    assert FakeAsyncClient.instances[0].closed is True
    assert FakeAsyncClient.response.closed is True


def test_food_cost_proxy_uses_analytics_menu_financial_scopes():
    session = _build_session(role="owner")
    FakeAsyncClient.instances = []
    FakeAsyncClient.response = FakeUpstreamResponse(chunks=[b"event: final\ndata: done\n\n"])

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.ai_agents.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.routers.ai_agents._resolve_member_id", AsyncMock(return_value=None)), \
         patch.object(ai_agents.settings, "agent_api_url", "http://agent-api:8100"), \
         patch.object(ai_agents.settings, "agent_internal_signature_secret", "secret"), \
         patch("app.routers.ai_agents.httpx.AsyncClient", FakeAsyncClient):
        response = _client().post("/ai/food-cost/messages/stream", content=BODY)

    assert response.status_code == 200
    sent = FakeAsyncClient.instances[0].sent_request
    assert sent["url"] == "http://agent-api:8100/internal/ai/food-cost/messages/stream"
    assert sent["headers"]["x-waro-scopes"] == "analytics:read,menu:read,financial:read"
    assert "x-waro-member-id" not in sent["headers"]


def test_missing_tenant_rejects_before_upstream_call():
    session = _build_session(role="owner", tenant_id=None)
    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.ai_agents.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.routers.ai_agents._resolve_member_id", AsyncMock()) as resolve_member, \
         patch("app.routers.ai_agents.httpx.AsyncClient", FakeAsyncClient):
        response = _client().post("/ai/sales/messages/stream", content=BODY)

    assert response.status_code == 401
    resolve_member.assert_not_awaited()


def test_missing_agent_config_rejects_before_upstream_call():
    session = _build_session(role="owner")
    FakeAsyncClient.instances = []

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.ai_agents.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.routers.ai_agents._resolve_member_id", AsyncMock(return_value=None)), \
         patch.object(ai_agents.settings, "agent_api_url", None), \
         patch.object(ai_agents.settings, "agent_internal_signature_secret", "secret"), \
         patch("app.routers.ai_agents.httpx.AsyncClient", FakeAsyncClient):
        response = _client().post("/ai/sales/messages/stream", content=BODY)

    assert response.status_code == 503
    assert FakeAsyncClient.instances == []


def test_module_gates_block_kitchen_role_before_upstream_call():
    session = _build_session(role="kitchen")
    kitchen_modules = frozenset({Module.DESPACHO})
    FakeAsyncClient.instances = []

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.core.permissions.get_role_modules", AsyncMock(return_value=kitchen_modules)), \
         patch("app.routers.ai_agents.httpx.AsyncClient", FakeAsyncClient):
        response = _client().post("/ai/sales/messages/stream", content=BODY)

    assert response.status_code == 403
    assert "ventas" in response.json()["detail"].lower()
    assert FakeAsyncClient.instances == []


def test_build_headers_generates_request_id_signature_and_content_headers():
    with patch.object(ai_agents.settings, "agent_internal_signature_secret", "secret"):
        headers = ai_agents._build_internal_headers(
            path="/internal/ai/food-cost/messages/stream",
            request_id="req-generated",
            tenant_id=str(TENANT_ID),
            profile_id=str(PROFILE_ID),
            member_id=None,
            scopes="analytics:read,menu:read,financial:read",
            body=BODY,
        )

    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"
    assert headers["x-waro-request-id"] == "req-generated"
    assert headers["x-waro-internal-signature"]
