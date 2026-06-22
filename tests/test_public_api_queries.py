from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.routers import public_api
from app.models.api_token import AVAILABLE_SCOPES
from app.services import public_api_service


def _request():
    return Request({"type": "http"})


def test_public_api_registers_queries_routes():
    paths = {route.path for route in public_api.router.routes}

    assert "/v1/queries/schema" in paths
    assert "/v1/queries/run" in paths


def test_public_api_registers_procurement_routes():
    paths = {route.path for route in public_api.router.routes}

    assert "/v1/inventory/stock" in paths
    assert "/v1/inventory/movements" in paths
    assert "/v1/purchases/direct" in paths
    assert "/v1/suppliers" in paths
    assert "suppliers:read" in AVAILABLE_SCOPES


@pytest.mark.asyncio
async def test_queries_schema_endpoint_requires_read_scope():
    request = _request()
    with patch(
        "app.routers.public_api.public_api_service.validate_api_key_auth",
        return_value=("tenant-1", "token-1"),
    ) as auth:
        result = await public_api.get_queries_schema(request)

    auth.assert_called_once_with(request, "read")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_queries_run_endpoint_delegates_to_queries_service():
    body = public_api.QuerySpecRequest(
        dataset="sales_items",
        measures=["revenue"],
        dimensions=["product"],
        filters={},
        order_by=[public_api.QueryOrderByRequest(field="revenue", direction="desc")],
        limit=10,
    )
    expected = {"success": True, "data": {"rows": []}}

    with patch(
        "app.routers.public_api.queries_service.run_queryspec",
        new=AsyncMock(return_value=expected),
    ) as run:
        request = _request()
        result = await public_api.run_queryspec(request, body)

    run.assert_awaited_once_with(request, body.dict())
    assert result == expected


@pytest.mark.asyncio
async def test_public_inventory_stock_uses_api_key_scope_without_session():
    request = _request()
    expected = {"success": True, "data": [], "total": 0}

    with patch(
        "app.services.public_api_service.validate_api_key_auth",
        return_value=("tenant-1", "token-1"),
    ) as auth, patch(
        "app.services.inventory_service.require_valid_session",
        side_effect=AssertionError("session auth should not be used"),
    ), patch(
        "app.services.inventory_service._get_inventory_stock_for_tenant",
        new=AsyncMock(return_value=expected),
    ) as core:
        result = await public_api_service.get_procurement_inventory_stock(
            request,
            limit=25,
            offset=5,
            search="tom",
            status_filter="low",
            category="Verduras",
            unit="gr",
            sort_field="ingredient_name",
            sort_direction="asc",
        )

    auth.assert_called_once_with(request, "inventory:read")
    core.assert_awaited_once_with(
        "tenant-1",
        limit=25,
        offset=5,
        search="tom",
        status_filter="low",
        category="Verduras",
        unit="gr",
        sort_field="ingredient_name",
        sort_direction="asc",
    )
    assert result["metadata"]["required_scope"] == "inventory:read"
    assert result["metadata"]["resource"] == "inventory_stock"


@pytest.mark.asyncio
async def test_public_inventory_movements_route_delegates_stable_body():
    ingredient_id = uuid4()
    body = public_api.ProcurementInventoryMovementsRequest(
        limit=30,
        offset=2,
        ingredientId=str(ingredient_id),
        movementType="purchase",
        quantityDirection="positive",
        startDate="2026-01-01",
        endDate="2026-01-31",
    )
    expected = {"success": True, "data": [], "metadata": {}}

    with patch(
        "app.routers.public_api.public_api_service.get_procurement_inventory_movements",
        new=AsyncMock(return_value=expected),
    ) as service:
        request = _request()
        result = await public_api.get_procurement_inventory_movements(request, body)

    service.assert_awaited_once_with(
        request,
        limit=30,
        offset=2,
        ingredient_id=ingredient_id,
        movement_type="purchase",
        quantity_direction="positive",
        start_date="2026-01-01",
        end_date="2026-01-31",
    )
    assert result == expected


@pytest.mark.asyncio
async def test_public_direct_purchases_uses_api_key_scope_without_session():
    request = _request()
    supplier_id = uuid4()

    with patch(
        "app.services.public_api_service.validate_api_key_auth",
        return_value=("tenant-1", "token-1"),
    ) as auth, patch(
        "app.services.direct_purchase_service.require_valid_session",
        side_effect=AssertionError("session auth should not be used"),
    ), patch(
        "app.services.direct_purchase_service._get_direct_purchases_for_tenant",
        new=AsyncMock(return_value={"success": True, "data": [], "total": 0}),
    ) as core:
        result = await public_api_service.get_procurement_direct_purchases(
            request,
            page=2,
            limit=10,
            search="fact",
            status="received",
            supplier_id=supplier_id,
            date_filter="last_week",
        )

    auth.assert_called_once_with(request, "purchases:read")
    core.assert_awaited_once_with(
        "tenant-1",
        page=2,
        limit=10,
        search="fact",
        status="received",
        supplier_id=supplier_id,
        date_filter="last_week",
    )
    assert result["metadata"]["required_scope"] == "purchases:read"
    assert result["metadata"]["resource"] == "direct_purchases"


@pytest.mark.asyncio
async def test_public_suppliers_uses_api_key_scope_without_session():
    request = _request()

    with patch(
        "app.services.public_api_service.validate_api_key_auth",
        return_value=("tenant-1", "token-1"),
    ) as auth, patch(
        "app.services.suppliers_service.require_valid_session",
        side_effect=AssertionError("session auth should not be used"),
    ), patch(
        "app.services.suppliers_service._get_suppliers_for_tenant",
        new=AsyncMock(return_value={"success": True, "data": [], "total": 0}),
    ) as core:
        result = await public_api_service.get_procurement_suppliers(
            request,
            page=1,
            limit=20,
            search="acme",
            search_field="name",
            is_active=True,
            payment_terms="30 dias",
        )

    auth.assert_called_once_with(request, "suppliers:read")
    core.assert_awaited_once_with(
        "tenant-1",
        page=1,
        limit=20,
        search="acme",
        search_field="name",
        is_active=True,
        payment_terms="30 dias",
    )
    assert result["metadata"]["required_scope"] == "suppliers:read"
    assert result["metadata"]["resource"] == "suppliers"
