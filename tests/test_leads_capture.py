"""Lead capture forwards optional visitor_key (uno0uno/waro-trail#4)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import leads as leads_router
from app.services.leads_service import capture_access_request, capture_lead


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(leads_router.router, prefix="/leads")
    return TestClient(app)


@asynccontextmanager
async def _fake_db():
    yield AsyncMock()


def test_capture_forwards_optional_visitor_key():
    capture = AsyncMock(return_value={"is_duplicate": False, "lead_id": "x", "profile_id": "y"})
    with (
        patch("app.routers.leads.get_db_connection", _fake_db),
        patch("app.routers.leads.leads_service.capture_lead", capture),
    ):
        response = _client().post(
            "/leads/capture",
            json={
                "email": "lead@example.com",
                "phone": "3001234567",
                "button_source": "comenzar",
                "visitor_key": "opaque-id",
            },
        )
    assert response.status_code == 200
    assert capture.await_args.kwargs["visitor_key"] == "opaque-id"


def test_capture_omits_blank_visitor_key():
    capture = AsyncMock(return_value={"is_duplicate": False, "lead_id": "x", "profile_id": "y"})
    with (
        patch("app.routers.leads.get_db_connection", _fake_db),
        patch("app.routers.leads.leads_service.capture_lead", capture),
    ):
        response = _client().post(
            "/leads/capture",
            json={
                "email": "lead@example.com",
                "phone": "3001234567",
                "visitor_key": "  ",
            },
        )
    assert response.status_code == 200
    assert capture.await_args.kwargs["visitor_key"] is None


def test_access_request_forwards_visitor_key():
    capture = AsyncMock(return_value={"lead_id": "x", "profile_id": "y"})
    with (
        patch("app.routers.leads.get_db_connection", _fake_db),
        patch("app.routers.leads.leads_service.capture_access_request", capture),
    ):
        response = _client().post(
            "/leads/access-request",
            json={"email": "lead@example.com", "visitor_key": "opaque-id"},
        )
    assert response.status_code == 200
    assert capture.await_args.kwargs["visitor_key"] == "opaque-id"


@pytest.mark.asyncio
async def test_capture_lead_inserts_visitor_key_column():
    profile_id = uuid4()
    lead_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": profile_id},
            None,
            {"id": lead_id},
        ]
    )
    conn.execute = AsyncMock()
    with patch("app.services.leads_service._send_notifications", new_callable=AsyncMock):
        await capture_lead(
            conn,
            email="lead@example.com",
            phone="3001234567",
            ip_address="1.1.1.1",
            user_agent="Mozilla/5.0",
            button_source="comenzar",
            visitor_key="opaque-id",
        )
    sql = conn.execute.await_args.args[0]
    assert "visitor_key" in sql
    assert conn.execute.await_args.args[6] == "opaque-id"


@pytest.mark.asyncio
async def test_access_request_inserts_visitor_key_column():
    profile_id = uuid4()
    lead_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": profile_id},
            {"id": lead_id},
        ]
    )
    conn.execute = AsyncMock()
    with patch("app.services.leads_service._send_access_request_notifications", new_callable=AsyncMock):
        await capture_access_request(
            conn,
            email="lead@example.com",
            phone=None,
            ip_address=None,
            user_agent=None,
            visitor_key="opaque-id",
        )
    sql = conn.execute.await_args.args[0]
    assert "visitor_key" in sql
    assert conn.execute.await_args.args[-1] == "opaque-id"
