from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.routers.tenant_config import router as tenant_config_router


@pytest.fixture(autouse=True)
def _clear_permission_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "owner@example.com",
        "name": "Owner",
        "expires_at": None,
        "is_active": True,
        "role": "owner",
    })


def _enforce_db_ctx():
    @asynccontextmanager
    async def _ctx():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetch = AsyncMock(return_value=[])
        yield conn
    return _ctx


def _fiscal_row(**overrides):
    row = {
        "nit": "900123456",
        "business_name": "Waro Test SAS",
        "type_organization_id": 1,
        "tax_regime_id": 2,
        "tax_level_id": 5,
        "fiscal_address": "Cra 1 # 2-3",
        "city": "Bogota",
        "city_id": 149,
        "phone": "3001234567",
        "email": "facturacion@example.com",
        "electronic_invoicing_requested": False,
        "matias_company_id": None,
        "receipt_document_label": "Prefactura",
        "receipt_tip_label": "Propina",
        "show_logo_on_receipts": True,
    }
    row.update(overrides)
    return row


def _build_app():
    app = FastAPI()
    app.include_router(tenant_config_router, prefix="/api/tenant")
    return app


def test_get_fiscal_data_returns_nullable_matias_company_id():
    session = _build_session()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_fiscal_row(matias_company_id=None))

    @asynccontextmanager
    async def fiscal_db_ctx():
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.database.get_db_connection", return_value=fiscal_db_ctx()):
        response = TestClient(_build_app()).get("/api/tenant/fiscal-data")

    assert response.status_code == 200
    assert response.json()["data"]["matias_company_id"] is None
    assert response.json()["data"]["electronic_invoicing_requested"] is False


def test_put_fiscal_data_persists_company_id_alias():
    session = _build_session()
    conn = MagicMock()
    conn.execute = AsyncMock()
    company_id = "8d4f2f79-4a4e-4d7d-bf07-7bd61c9e4f37"

    @asynccontextmanager
    async def fiscal_db_ctx():
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.database.get_db_connection", return_value=fiscal_db_ctx()):
        response = TestClient(_build_app()).put(
            "/api/tenant/fiscal-data",
            json={
                "nit": "900123456",
                "business_name": "Waro Test SAS",
                "companyId": f"  {company_id}  ",
            },
        )

    assert response.status_code == 200
    execute_args = conn.execute.await_args.args
    assert "matias_company_id" in execute_args[0]
    assert execute_args[12] is False
    assert execute_args[13] == company_id


def test_put_fiscal_data_persists_client_uuid_alias():
    session = _build_session()
    conn = MagicMock()
    conn.execute = AsyncMock()
    company_id = "8d4f2f79-4a4e-4d7d-bf07-7bd61c9e4f37"

    @asynccontextmanager
    async def fiscal_db_ctx():
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.database.get_db_connection", return_value=fiscal_db_ctx()):
        response = TestClient(_build_app()).put(
            "/api/tenant/fiscal-data",
            json={
                "nit": "900123456",
                "business_name": "Waro Test SAS",
                "client_uuid": f"  {company_id}  ",
            },
        )

    assert response.status_code == 200
    execute_args = conn.execute.await_args.args
    assert execute_args[12] is False
    assert execute_args[13] == company_id


def test_put_fiscal_data_normalizes_blank_matias_company_id_to_null():
    session = _build_session()
    conn = MagicMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def fiscal_db_ctx():
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.database.get_db_connection", return_value=fiscal_db_ctx()):
        response = TestClient(_build_app()).put(
            "/api/tenant/fiscal-data",
            json={"nit": "900123456", "matias_company_id": "   "},
        )

    assert response.status_code == 200
    assert conn.execute.await_args.args[13] is None


def test_put_fiscal_data_persists_electronic_invoicing_request_without_internal_flag():
    session = _build_session()
    conn = MagicMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def fiscal_db_ctx():
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.database.get_db_connection", return_value=fiscal_db_ctx()):
        response = TestClient(_build_app()).put(
            "/api/tenant/fiscal-data",
            json={
                "nit": "900123456",
                "business_name": "Waro Test SAS",
                "electronic_invoicing_requested": True,
                "electronic_invoicing_enabled": True,
            },
        )

    assert response.status_code == 200
    execute_args = conn.execute.await_args.args
    sql = execute_args[0]
    assert "electronic_invoicing_requested" in sql
    assert "electronic_invoicing_enabled" not in sql
    assert execute_args[12] is True


def test_put_fiscal_data_keeps_tax_config_separate_for_no_responsable_persona_natural():
    session = _build_session()
    conn = MagicMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def fiscal_db_ctx():
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.database.get_db_connection", return_value=fiscal_db_ctx()):
        response = TestClient(_build_app()).put(
            "/api/tenant/fiscal-data",
            json={
                "nit": "123456789",
                "business_name": "Persona Natural Test",
                "type_organization_id": 2,
                "tax_regime_id": 2,
                "tax_level_id": 5,
                "inc_applicable": True,
                "iva_applicable": True,
            },
        )

    assert response.status_code == 200
    execute_args = conn.execute.await_args.args
    sql = execute_args[0]
    assert "tenant_fiscal_data" in sql
    assert "tenant_tax_config" not in sql
    assert "inc_applicable" not in sql
    assert "iva_applicable" not in sql
    assert execute_args[4] == 2
    assert execute_args[5] == 2
    assert execute_args[6] == 5


def test_put_fiscal_data_rejects_invalid_matias_company_id():
    session = _build_session()

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()):
        response = TestClient(_build_app()).put(
            "/api/tenant/fiscal-data",
            json={"matias_company_id": "not-a-waro-tenant-id"},
        )

    assert response.status_code == 400
    assert "Matias companyId" in response.json()["detail"]
