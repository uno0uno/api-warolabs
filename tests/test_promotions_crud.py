"""Scope resolution and promotion router smoke tests (warocol.com#980)."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.models.tenant_promotion import PromotionCreate, PromoType, ScopeType
from app.routers.promotions import router as promotions_router
from app.services.promotions_service import product_in_scope


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


def test_bogo_create_requires_buy_and_get_qty():
    with pytest.raises(ValidationError):
        PromotionCreate(
            name="2x1",
            promo_type=PromoType.BOGO,
            value_json={"buy_qty": 2},
            scope_type=ScopeType.ALL_PRODUCTS,
        )


def test_bogo_create_valid():
    body = PromotionCreate(
        name="2x1 cervezas",
        promo_type=PromoType.BOGO,
        value_json={"buy_qty": 2, "get_qty": 1},
        scope_type=ScopeType.ALL_PRODUCTS,
    )
    assert body.promo_type == PromoType.BOGO


def test_product_in_scope_all_products():
    pid = uuid4()
    cid = uuid4()
    assert product_in_scope(
        scope_type="all_products",
        category_ids=set(),
        product_ids=set(),
        product_id=pid,
        category_id=cid,
    )


def test_product_in_scope_by_product_id():
    pid = uuid4()
    other = uuid4()
    assert product_in_scope(
        scope_type="products",
        category_ids=set(),
        product_ids={pid},
        product_id=pid,
        category_id=None,
    )
    assert not product_in_scope(
        scope_type="products",
        category_ids=set(),
        product_ids={pid},
        product_id=other,
        category_id=None,
    )


def test_product_in_scope_by_category():
    pid = uuid4()
    cid = uuid4()
    assert product_in_scope(
        scope_type="categories",
        category_ids={cid},
        product_ids=set(),
        product_id=pid,
        category_id=cid,
    )


def test_owner_passes_list_promotions_under_enforce():
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(promotions_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.services.promotions_service.list_promotions",
             new=AsyncMock(return_value={"success": True, "total": 0, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/api/promotions")

    assert response.status_code == 200


def test_cashier_passes_active_promotions_under_enforce():
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(promotions_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS, Module.MENU})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.services.promotions_service.list_active_promotions",
             new=AsyncMock(return_value={"success": True, "total": 0, "data": []}),
         ):
        client = TestClient(app)
        response = client.get(
            "/api/promotions/active",
            params={"at": datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc).isoformat()},
        )

    assert response.status_code == 200


def test_cashier_denied_create_promotion_under_enforce():
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(promotions_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.post(
            "/api/promotions",
            json={
                "name": "2x1",
                "promo_type": "bogo",
                "value_json": {"buy_qty": 2, "get_qty": 1},
                "scope_type": "all_products",
            },
        )

    assert response.status_code == 403
