"""End-to-end smoke tests for POS group endpoints under require_module(POS).

Sub-task E2.3 of Epic 2 (#188). Validates that:
1. Owner role under enforce reaches the handler.
2. Kitchen role under enforce gets 403 (kitchen lacks POS in default matrix).
3. KDS-direct synthetic session (role=None) passes the EXCLUDED comandas
   endpoints — proves the `# NOTE:` exclusions actually leave them ungated.

Pairs with `test_billing_permissions.py` (#185 / E2.14 reference impl).
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.tables import router as tables_router
from app.routers.comandas import router as comandas_router
from app.routers.pos_context import router as pos_context_router
from app.routers.pos_products import router as pos_products_router
from app.routers.pos_customers import router as pos_customers_router
from app.routers.pos_orders import router as pos_orders_router
from app.models.product import Product, ProductResponse


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role):
    """Build a SessionContext with the given role."""
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
    """Async context manager whose .fetchval returns 'enforce'."""
    @asynccontextmanager
    async def _ctx():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetch = AsyncMock(return_value=[])
        yield conn
    return _ctx


# ─── Tests ────────────────────────────────────────────────────────────


def test_owner_role_passes_pos_endpoint_under_enforce():
    """Owner reaches GET /tables under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(tables_router, prefix="/tables")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.tables.tables_service.list_tables",
             new=AsyncMock(return_value={"tables": []}),
         ):
        client = TestClient(app)
        response = client.get("/tables")

    # Handler reached → dependency permitted.
    assert response.status_code == 200


def test_kitchen_role_denied_pos_endpoint_under_enforce():
    """Kitchen role hits 403 on POS endpoint — kitchen lacks POS in default matrix."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(tables_router, prefix="/tables")

    # Stub get_role_modules to return kitchen's default set (DESPACHO only).
    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/tables")

    assert response.status_code == 403
    assert "pos" in response.json()["detail"].lower()


def test_kds_synthetic_session_passes_excluded_comandas_endpoint():
    """KDS-style session (role=None) passes the EXCLUDED `GET /comandas`.

    The endpoint at comandas.py line 86 (now ~line 90 after the # NOTE:
    block) carries no `Depends(require_module)`. With `role=None` and
    enforce mode, the dependency would 403 — but it's not registered, so
    the request reaches the handler.
    """
    session = _build_session(role=None)
    app = FastAPI()
    app.include_router(comandas_router, prefix="/api/comandas")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch(
             "app.routers.comandas.comandas_service.get_comandas_for_kds",
             new=AsyncMock(return_value={"data": []}),
         ):
        client = TestClient(app)
        response = client.get("/api/comandas")

    # No 403 — handler reached, role=None passed because endpoint is ungated.
    assert response.status_code == 200


def test_cashier_role_passes_pos_restaurant_context_under_enforce():
    """Cashier reaches GET /pos/restaurant-context under enforce.

    Validates the BFF scoped endpoint introduced in E2.7/E2.15 — POS pages
    consume tenant context from this aggregator instead of /api/tenant/*,
    so cashiers do NOT need MI_NEGOCIO (which stays owner-only).
    """
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(pos_context_router)

    cashier_modules = frozenset({Module.POS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.pos_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.pos_context.get_restaurant_context",
             new=AsyncMock(return_value={
                 "display_name": "Demo",
                 "kds_enabled": True,
                 "comandas_enabled": True,
                 "expediter_enabled": False,
                 "fiscal_data": {"nit": "900000000"},
                 "tax_config": {"iva_applicable": False},
                 "invoicing_ready": False,
                 "timezone": "America/Bogota",
             }),
         ):
        client = TestClient(app)
        response = client.get("/pos/restaurant-context")

    assert response.status_code == 200
    assert response.json()["data"]["display_name"] == "Demo"
    assert response.json()["data"]["timezone"] == "America/Bogota"


def test_cashier_role_passes_pos_products_under_enforce_without_menu():
    """Cashier reaches GET /pos/products with POS access but no MENU module."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(pos_products_router)

    cashier_modules = frozenset({Module.POS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.pos_products.get_products_list",
             new=AsyncMock(return_value={"success": True, "data": [], "total": 0}),
         ) as get_products:
        client = TestClient(app)
        response = client.get("/pos/products?is_available=true&limit=250&include_modifiers=true")

    assert response.status_code == 200
    get_products.assert_awaited_once()
    assert get_products.await_args.args[7] is True
    assert get_products.await_args.args[17] is True


def test_kitchen_role_denied_pos_products_under_enforce():
    """Kitchen role hits 403 on POS catalog endpoint because it lacks POS."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(pos_products_router)

    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ), \
         patch(
             "app.routers.pos_products.get_products_list",
             new=AsyncMock(return_value={"success": True, "data": [], "total": 0}),
         ) as get_products:
        client = TestClient(app)
        response = client.get("/pos/products")

    assert response.status_code == 403
    assert "pos" in response.json()["detail"].lower()
    get_products.assert_not_awaited()


def _minimal_product_response(product_id):
    from datetime import datetime, timezone
    from decimal import Decimal

    now = datetime.now(timezone.utc)
    return ProductResponse(
        data=Product(
            id=product_id,
            tenant_id=uuid4(),
            name="Test Product",
            price=Decimal("10000"),
            category_id=uuid4(),
            created_at=now,
            updated_at=now,
        )
    )


def test_cashier_role_passes_pos_product_detail_under_enforce():
    """Cashier reaches GET /pos/products/{id} under enforce without MENU."""
    session = _build_session(role="cashier")
    product_id = uuid4()
    app = FastAPI()
    app.include_router(pos_products_router)

    cashier_modules = frozenset({Module.POS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.pos_products.get_product_by_id",
             new=AsyncMock(return_value=_minimal_product_response(product_id)),
         ) as get_one:
        client = TestClient(app)
        response = client.get(f"/pos/products/{product_id}")

    assert response.status_code == 200
    get_one.assert_awaited_once()
    assert response.json()["data"]["name"] == "Test Product"


def test_kitchen_role_denied_pos_product_detail_under_enforce():
    """Kitchen role hits 403 on POS product detail because it lacks POS."""
    session = _build_session(role="kitchen")
    product_id = uuid4()
    app = FastAPI()
    app.include_router(pos_products_router)

    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ), \
         patch(
             "app.routers.pos_products.get_product_by_id",
             new=AsyncMock(return_value=_minimal_product_response(product_id)),
         ) as get_one:
        client = TestClient(app)
        response = client.get(f"/pos/products/{product_id}")

    assert response.status_code == 403
    assert "pos" in response.json()["detail"].lower()
    get_one.assert_not_awaited()


def test_cashier_role_passes_pos_customer_search_or_create_under_enforce():
    """Cashier reaches POST /pos/customers/search-or-create without VENTAS."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(pos_customers_router)

    cashier_modules = frozenset({Module.POS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.pos_customers.search_or_create_customer",
             new=AsyncMock(return_value={
                 "success": True,
                 "data": {
                     "id": str(uuid4()),
                     "phone_number": "3001234567",
                     "created_at": datetime.now(timezone.utc).isoformat(),
                 },
                 "is_new": False,
             }),
         ) as search_or_create:
        client = TestClient(app)
        response = client.post(
            "/pos/customers/search-or-create",
            json={"phone_number": "3001234567", "name": "Test"},
        )

    assert response.status_code == 200
    search_or_create.assert_awaited_once()


def test_cashier_role_passes_pos_order_invoice_read_under_enforce():
    """Cashier reaches GET /pos/orders/{id}/invoice without VENTAS."""
    session = _build_session(role="cashier")
    order_id = uuid4()
    app = FastAPI()
    app.include_router(pos_orders_router)

    cashier_modules = frozenset({Module.POS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.pos_orders.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.pos_orders.facturacion_service.get_order_invoice",
             new=AsyncMock(return_value={"status": "accepted", "prefix": "FE", "invoice_number": 1}),
         ) as get_invoice:
        client = TestClient(app)
        response = client.get(f"/pos/orders/{order_id}/invoice")

    assert response.status_code == 200
    get_invoice.assert_awaited_once()


def test_kitchen_role_denied_pos_customers_under_enforce():
    """Kitchen role hits 403 on POS customer endpoints because it lacks POS."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(pos_customers_router)

    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ), \
         patch(
             "app.routers.pos_customers.search_or_create_customer",
             new=AsyncMock(),
         ) as search_or_create:
        client = TestClient(app)
        response = client.post(
            "/pos/customers/search-or-create",
            json={"phone_number": "3001234567"},
        )

    assert response.status_code == 403
    assert "pos" in response.json()["detail"].lower()
    search_or_create.assert_not_awaited()
