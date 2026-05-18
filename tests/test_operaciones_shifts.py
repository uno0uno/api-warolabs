"""Tests for /operaciones/shifts shift template CRUD (warocol.com#682)."""
from contextlib import asynccontextmanager
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.models.shift_template import ShiftTemplateCreate, ShiftTemplatePatch
from app.routers.operaciones_shifts import router as operaciones_shifts_router


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role: str):
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


def test_shift_template_create_rejects_invalid_same_day_window():
  with pytest.raises(ValidationError):
    ShiftTemplateCreate(
      name="Mañana",
      start_time=time(14, 0),
      end_time=time(6, 0),
      crosses_midnight=False,
    )


def test_shift_template_create_allows_overnight_window():
  body = ShiftTemplateCreate(
    name="Noche",
    start_time=time(22, 0),
    end_time=time(6, 0),
    crosses_midnight=True,
  )
  assert body.crosses_midnight is True


def test_supervisor_passes_list_shifts_under_enforce():
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_shifts_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.DESPACHO, Module.MENU,
        Module.OPERACIONES, Module.ABASTECIMIENTO, Module.ANALITICA,
    })

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.services.shift_templates_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ), \
         patch(
             "app.services.shift_templates_service.list_shift_templates",
             new=AsyncMock(return_value={"success": True, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/operaciones/shifts")

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_kitchen_denied_list_shifts_under_enforce():
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(operaciones_shifts_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.services.shift_templates_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=frozenset({Module.DESPACHO})),
         ):
        client = TestClient(app)
        response = client.get("/operaciones/shifts")

    assert response.status_code == 403
    assert "operaciones" in response.json()["detail"].lower()


def test_create_endpoint_passes_body_to_service():
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(operaciones_shifts_router)
    create_mock = AsyncMock(return_value={"success": True, "data": {"id": str(uuid4())}})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.services.shift_templates_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=frozenset(Module)),
         ), \
         patch(
             "app.services.shift_templates_service.create_shift_template",
             create_mock,
         ):
        client = TestClient(app)
        response = client.post(
            "/operaciones/shifts",
            json={
                "name": "Mañana",
                "start_time": "06:00:00",
                "end_time": "14:00:00",
                "crosses_midnight": False,
                "sort_order": 0,
            },
        )

    assert response.status_code == 200
    create_mock.assert_awaited_once()
    body_arg = create_mock.await_args.args[1]
    assert body_arg.name == "Mañana"


def test_patch_model_accepts_deactivate_only():
    body = ShiftTemplatePatch(is_active=False)
    assert body.is_active is False
