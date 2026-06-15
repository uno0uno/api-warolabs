from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.middleware import SessionContext
from app.routers.legal import router
from app.services import legal_service
from app.services import aws_s3_service
from app.services.aws_s3_service import AWSS3Service


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
async def test_public_asset_upload_uses_public_r2_bucket(monkeypatch):
    calls = {}

    class FakeClient:
        def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
            calls["bucket"] = bucket
            calls["key"] = key
            calls["extra_args"] = ExtraArgs
            calls["bytes"] = fileobj.read()

    monkeypatch.setattr(aws_s3_service.settings, "r2_public_url", "https://pub.example")
    monkeypatch.setattr(aws_s3_service.settings, "r2_public_bucket", "warocol-public-assets")
    monkeypatch.setattr(aws_s3_service.settings, "r2_access_key_id", "key")
    monkeypatch.setattr(aws_s3_service.settings, "r2_secret_access_key", "secret")
    monkeypatch.setattr(aws_s3_service.settings, "r2_endpoint", "https://r2.example")
    monkeypatch.setattr(aws_s3_service.boto3, "client", lambda *args, **kwargs: FakeClient())

    service = object.__new__(AWSS3Service)
    url = await service.upload_public_asset(
        b"pdf",
        "legal/terms/TyC_WARO_v1.1.pdf",
        "application/pdf",
        metadata={"version": "1.1"},
    )

    assert url == "https://pub.example/legal/terms/TyC_WARO_v1.1.pdf"
    assert calls["bucket"] == "warocol-public-assets"
    assert calls["key"] == "legal/terms/TyC_WARO_v1.1.pdf"
    assert calls["extra_args"]["ContentType"] == "application/pdf"
    assert calls["extra_args"]["Metadata"]["version"] == "1.1"
    assert calls["bytes"] == b"pdf"


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
async def test_acceptance_audit_list_is_tenant_scoped_and_filterable():
    tenant_id = uuid4()
    version_id = uuid4()
    row = _acceptance_row(tenant_id, version_id)
    accepted_from = datetime(2026, 6, 1, tzinfo=timezone.utc)
    accepted_to = datetime(2026, 6, 30, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])

    result = await legal_service.list_acceptance_audit_records(
        conn,
        tenant_id,
        document_version_id=version_id,
        actor_email="test@warocol.com",
        accepted_from=accepted_from,
        accepted_to=accepted_to,
        limit=25,
        offset=5,
    )

    assert result["data"]["records"][0]["tenant_id"] == str(tenant_id)
    assert result["data"]["records"][0]["document_version_id"] == str(version_id)
    query_args = conn.fetch.await_args.args
    assert query_args[1] == tenant_id
    assert query_args[2] == version_id
    assert query_args[3] == "test@warocol.com"
    assert query_args[4] == accepted_from
    assert query_args[5] == accepted_to
    assert query_args[6] == 25
    assert query_args[7] == 5


@pytest.mark.asyncio
async def test_acceptance_audit_detail_requires_tenant_match():
    tenant_id = uuid4()
    acceptance_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await legal_service.get_acceptance_audit_record(conn, tenant_id, acceptance_id)

    assert exc_info.value.status_code == 404
    query_args = conn.fetchrow.await_args.args
    assert query_args[1] == tenant_id
    assert query_args[2] == acceptance_id


@pytest.mark.asyncio
async def test_current_version_query_filters_to_published_effective_versions():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    result = await legal_service.get_current_terms(conn, uuid4())

    assert result is None
    query = conn.fetchrow.await_args.args[0]
    assert "v.status = 'published'" in query
    assert "v.effective_at <= now()" in query


@pytest.mark.asyncio
async def test_publish_terms_version_upserts_without_touching_acceptances():
    version_id = uuid4()
    document_id = uuid4()
    now = datetime(2026, 6, 15, 5, 0, tzinfo=timezone.utc)
    final_row = {
        "document_id": document_id,
        "document_code": "terms_conditions",
        "document_title": "Terminos y Condiciones WARO",
        "retention_years": 10,
        "version_id": version_id,
        "version": "1.1",
        "effective_at": now,
        "published_at": now,
        "content_url": "https://pub.example/legal/terms/TyC_WARO_v1.1.pdf",
        "content_sha256": "abc123",
        "metadata": {"display_mode": "pdf"},
    }
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=document_id)
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"id": version_id},
        final_row,
    ])

    result = await legal_service.publish_terms_version(
        conn,
        version="1.1",
        effective_at=now,
        content_url=final_row["content_url"],
        content_sha256="abc123",
        metadata={"display_mode": "pdf"},
    )

    assert result["version"] == "1.1"
    assert result["content_url"] == final_row["content_url"]
    assert result["metadata"]["display_mode"] == "pdf"


@pytest.mark.asyncio
async def test_publish_terms_version_rejects_hash_change_after_acceptance():
    version_id = uuid4()
    document_id = uuid4()
    now = datetime(2026, 6, 15, 5, 0, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=document_id)
    conn.fetchrow = AsyncMock(return_value={
        "id": version_id,
        "content_sha256": "old-hash",
        "acceptance_count": 1,
    })

    with pytest.raises(HTTPException) as exc_info:
        await legal_service.publish_terms_version(
            conn,
            version="1.1",
            effective_at=now,
            content_url="https://pub.example/legal/terms/TyC_WARO_v1.1.pdf",
            content_sha256="new-hash",
            metadata={"display_mode": "pdf"},
        )

    assert exc_info.value.status_code == 409


def test_legal_acceptance_migrations_preserve_and_lock_evidence():
    migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
    schema_sql = (migrations_dir / "085_legal_terms_acceptance.sql").read_text()
    immutability_sql = (migrations_dir / "086_legal_acceptance_immutability.sql").read_text()

    assert "retention_years INTEGER NOT NULL DEFAULT 10 CHECK (retention_years >= 10)" in schema_sql
    assert "tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT" in schema_sql
    assert "document_version_id UUID NOT NULL REFERENCES legal_document_versions(id) ON DELETE RESTRICT" in schema_sql
    assert "BEFORE UPDATE OR DELETE ON tenant_legal_acceptances" in immutability_sql
    assert "retain acceptance evidence for at least 10 years" in immutability_sql


def test_accept_endpoint_uses_forwarded_ip_and_user_agent():
    app = FastAPI()
    app.include_router(router)
    session = _session()

    @asynccontextmanager
    async def _db(**_kwargs):
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


def test_audit_endpoint_returns_tenant_scoped_evidence():
    app = FastAPI()
    app.include_router(router)
    session = _session()

    @asynccontextmanager
    async def _db(**_kwargs):
        yield MagicMock()

    audit_payload = {
        "success": True,
        "data": {
            "records": [_acceptance_row(session.tenant_id, uuid4())],
            "limit": 10,
            "offset": 0,
        },
    }

    with patch("app.routers.legal.require_valid_session", return_value=session), \
         patch("app.routers.legal.get_db_connection", side_effect=_db), \
         patch("app.routers.legal.legal_service.list_acceptance_audit_records", new=AsyncMock(return_value=audit_payload)) as audit_mock:
        client = TestClient(app)
        res = client.get("/legal/terms/audit?limit=10")

    assert res.status_code == 200
    assert res.json()["success"] is True
    _, args, kwargs = audit_mock.mock_calls[0]
    assert args[1] == session.tenant_id
    assert kwargs["limit"] == 10
