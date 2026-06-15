from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.middleware import SessionContext
from app.routers.legal import router
from app.services import legal_service


def _version_row(version="1.0"):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    return {
        "document_id": uuid4(),
        "document_code": "terms_conditions",
        "document_title": "Terminos y Condiciones WARO",
        "retention_years": 10,
        "version_id": uuid4(),
        "version": version,
        "effective_at": now,
        "published_at": now,
        "content_url": "/terminos-y-condiciones",
        "content_sha256": None,
        "metadata": {},
    }


def _acceptance_row(tenant_id, version_id):
    now = datetime(2026, 6, 15, 12, 5, tzinfo=timezone.utc)
    return {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "document_version_id": version_id,
        "user_id": uuid4(),
        "source": "billing_checkout",
        "accepted_at": now,
        "client_ip": "203.0.113.10",
        "user_agent": "pytest-agent",
        "tenant_name_snapshot": "Waro Colombia",
        "legal_name_snapshot": "Waro Colombia SAS",
        "document_type_snapshot": "NIT",
        "document_number_snapshot": "900123456",
        "email_snapshot": "legal@warocol.com",
        "actor_name_snapshot": "Test User",
        "actor_email_snapshot": "test@warocol.com",
        "document_code_snapshot": "terms_conditions",
        "document_title_snapshot": "Terminos y Condiciones WARO",
        "version_snapshot": "1.0",
        "annexes_snapshot": [],
        "evidence": {"retention_years": 10},
    }


def _session(tenant_id=None):
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": tenant_id or uuid4(),
        "email": "test@warocol.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": "owner",
    })


@pytest.mark.asyncio
async def test_status_detects_missing_acceptance_for_current_version():
    tenant_id = uuid4()
    version = _version_row()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[version, None])
    conn.fetch = AsyncMock(return_value=[])

    result = await legal_service.get_terms_status(conn, tenant_id)

    assert result["data"]["requires_acceptance"] is True
    assert result["data"]["current"]["version"] == "1.0"
    assert result["data"]["acceptance"] is None


@pytest.mark.asyncio
async def test_status_requires_tenant():
    conn = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await legal_service.get_terms_status(conn, None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_accept_current_terms_captures_evidence_snapshot():
    session = _session()
    version = _version_row()
    acceptance = _acceptance_row(session.tenant_id, version["version_id"])
    snapshot = {
        "tenant_name": "Waro Colombia",
        "tenant_email": "tenant@warocol.com",
        "legal_name": "Waro Colombia SAS",
        "document_number": "900123456",
        "email": "legal@warocol.com",
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[version, None, snapshot, acceptance])
    conn.fetch = AsyncMock(return_value=[])

    result = await legal_service.accept_current_terms(
        conn,
        session,
        client_ip="203.0.113.10",
        user_agent="pytest-agent",
        source="billing_checkout",
    )

    assert result["data"]["already_accepted"] is False
    assert result["data"]["acceptance"]["client_ip"] == "203.0.113.10"
    assert result["data"]["acceptance"]["document_number"] == "900123456"
    insert_args = conn.fetchrow.await_args_list[-1].args
    assert insert_args[4] == "billing_checkout"
    assert insert_args[5] == "203.0.113.10"
    assert insert_args[6] == "pytest-agent"
    assert insert_args[9] == "900123456"


@pytest.mark.asyncio
async def test_accept_current_terms_is_idempotent_for_same_version():
    session = _session()
    version = _version_row()
    existing = _acceptance_row(session.tenant_id, version["version_id"])
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[version, existing])
    conn.fetch = AsyncMock(return_value=[])

    result = await legal_service.accept_current_terms(
        conn,
        session,
        client_ip="203.0.113.10",
        user_agent="pytest-agent",
    )

    assert result["data"]["already_accepted"] is True
    assert result["data"]["acceptance"]["id"] == str(existing["id"])
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_current_version_query_filters_to_published_effective_versions():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    result = await legal_service.get_current_terms(conn, uuid4())

    assert result is None
    query = conn.fetchrow.await_args.args[0]
    assert "v.status = 'published'" in query
    assert "v.effective_at <= now()" in query


def test_accept_endpoint_uses_forwarded_ip_and_user_agent():
    app = FastAPI()
    app.include_router(router)
    session = _session()

    @asynccontextmanager
    async def _db():
        yield MagicMock()

    with patch("app.routers.legal.require_valid_session", return_value=session), \
         patch("app.routers.legal.get_db_connection", side_effect=_db), \
         patch("app.routers.legal.legal_service.accept_current_terms", new=AsyncMock(return_value={"success": True, "data": {}})) as accept_mock:
        client = TestClient(app)
        res = client.post(
            "/legal/terms/accept",
            json={"source": "billing_checkout"},
            headers={
                "x-forwarded-for": "198.51.100.10, 10.0.0.1",
                "user-agent": "pytest-browser",
            },
        )

    assert res.status_code == 201
    _, _, kwargs = accept_mock.mock_calls[0]
    assert kwargs["client_ip"] == "198.51.100.10"
    assert kwargs["user_agent"] == "pytest-browser"
    assert kwargs["source"] == "billing_checkout"
