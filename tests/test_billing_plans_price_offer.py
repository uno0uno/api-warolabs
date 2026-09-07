"""GET /billing/plans includes regional price_offer (#796) + country filter (#2201)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.routers.billing import tenant_router
from app.services.billing_service import (
    ELECTRONIC_INVOICE_PLAN_SLUG,
    assert_plan_available_for_country,
    filter_plans_for_country,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _plans_catalog():
    return [
        {"id": str(uuid4()), "name": "Starter", "slug": "starter", "is_active": True},
        {"id": str(uuid4()), "name": "Pro", "slug": "pro", "is_active": True},
        {
            "id": str(uuid4()),
            "name": "Facturación electrónica",
            "slug": ELECTRONIC_INVOICE_PLAN_SLUG,
            "is_active": True,
        },
    ]


def test_filter_plans_for_country_hides_fe_outside_co():
    plans = _plans_catalog()
    intl = filter_plans_for_country(plans, "US")
    assert [p["slug"] for p in intl] == ["starter", "pro"]
    co = filter_plans_for_country(plans, "CO")
    assert [p["slug"] for p in co] == ["starter", "pro", ELECTRONIC_INVOICE_PLAN_SLUG]


def test_assert_plan_available_for_country_blocks_fe_outside_co():
    assert_plan_available_for_country(ELECTRONIC_INVOICE_PLAN_SLUG, "CO")
    with pytest.raises(HTTPException) as exc:
        assert_plan_available_for_country(ELECTRONIC_INVOICE_PLAN_SLUG, "US")
    assert exc.value.status_code == 422


def _session(tenant_id):
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": tenant_id,
        "email": "test@example.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
        "role": "owner",
    })


def _db_cm():
    @asynccontextmanager
    async def _db(**_kwargs):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="disabled")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    return _db


def test_tenant_list_plans_includes_price_offer_for_co():
    tenant_id = uuid4()
    session = _session(tenant_id)
    plans = _plans_catalog()

    app = FastAPI()
    app.include_router(tenant_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_db_cm()), \
         patch("app.routers.billing.get_db_connection", side_effect=_db_cm()), \
         patch("app.routers.billing.billing_service.list_plans", new=AsyncMock(return_value=plans)), \
         patch(
             "app.routers.billing.billing_service.get_tenant_billing_context",
             new=AsyncMock(return_value={"slug": "demo", "country_code": "CO"}),
         ):
        client = TestClient(app)
        res = client.get("/billing/plans")

    assert res.status_code == 200
    body = res.json()
    assert [p["slug"] for p in body["plans"]] == [
        "starter",
        "pro",
        ELECTRONIC_INVOICE_PLAN_SLUG,
    ]
    assert body["price_offer"]["currency"] == "USD"
    assert body["price_offer"]["segment"] == "usd_9"
    assert body["price_offer"]["annual_amount_minor"] == 9000
    assert body["price_offer"]["annual_amount"] == 90.0


def test_tenant_list_plans_omits_fe_for_non_co():
    tenant_id = uuid4()
    session = _session(tenant_id)
    plans = _plans_catalog()

    app = FastAPI()
    app.include_router(tenant_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_db_cm()), \
         patch("app.routers.billing.get_db_connection", side_effect=_db_cm()), \
         patch("app.routers.billing.billing_service.list_plans", new=AsyncMock(return_value=plans)), \
         patch(
             "app.routers.billing.billing_service.get_tenant_billing_context",
             new=AsyncMock(return_value={"slug": "test-23-07-2026", "country_code": "US"}),
         ):
        client = TestClient(app)
        res = client.get("/billing/plans")

    assert res.status_code == 200
    body = res.json()
    assert [p["slug"] for p in body["plans"]] == ["starter", "pro"]
    assert body["price_offer"]["segment"] == "usd_30"
