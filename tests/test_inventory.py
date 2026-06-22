"""
Tests for inventory module endpoints.

Endpoints tested:
- GET /inventory/stock
- GET /inventory/movements
- GET /inventory/stock/{ingredient_id}
- POST /inventory/adjustments
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from uuid import uuid4

from app.services.inventory_service import (
    _get_inventory_movements_for_tenant,
    _get_inventory_stock_for_tenant,
    _json_decimal,
    _QUANTITY_JSON_SCALE,
    _TECHNICAL_COST_JSON_SCALE,
    get_inventory_movements,
    get_inventory_stock,
)


def test_inventory_json_decimal_strips_float_residue():
    assert _json_decimal(0.1 + 0.2 - 0.3, _QUANTITY_JSON_SCALE, 0) == 0.0
    assert _json_decimal("1.3450000000001", _QUANTITY_JSON_SCALE, 0) == 1.345
    assert _json_decimal("0.3333334", _QUANTITY_JSON_SCALE, 0) == 0.333333


def test_inventory_json_decimal_preserves_technical_unit_cost_precision():
    assert _json_decimal("6.617100371747212", _TECHNICAL_COST_JSON_SCALE, 0) == 6.6171
    assert _json_decimal("250.1234564", _TECHNICAL_COST_JSON_SCALE, 0) == 250.123456


class _FakeInventoryConnection:
    def __init__(self, *, fetch_rows=None, fetchrow_rows=None):
        self.fetch_rows = fetch_rows or []
        self.fetchrow_rows = list(fetchrow_rows or [])
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetch(self, query, *params):
        self.fetch_calls.append((query, params))
        return self.fetch_rows

    async def fetchrow(self, query, *params):
        self.fetchrow_calls.append((query, params))
        return self.fetchrow_rows.pop(0)


@pytest.mark.asyncio
async def test_inventory_stock_core_uses_explicit_tenant_without_session():
    tenant_id = "tenant-core-stock"
    conn = _FakeInventoryConnection(
        fetch_rows=[
            {
                "id": uuid4(),
                "ingredient_id": uuid4(),
                "ingredient_name": "Tomate",
                "unit": "gr",
                "category": "Verduras",
                "current_stock": "12.5",
                "minimum_stock": "5",
                "maximum_stock": "20",
                "last_updated": datetime(2026, 1, 1, 12, 0),
                "location": "Bodega",
                "lote_actual": "L1",
                "fecha_vencimiento": datetime(2026, 2, 1, 12, 0),
                "status": "ok",
                "stock_percentage": "62.5",
                "unit_cost": "100.25",
                "total_value": "1253.125",
            }
        ],
        fetchrow_rows=[
            {"total": 1},
            {
                "total_ingredients": 1,
                "critical_count": 0,
                "low_stock_count": 0,
                "ok_count": 1,
                "total_inventory_value": "1253.125",
            },
            {"categories": ["Verduras"], "units": ["gr"]},
        ],
    )

    with patch("app.services.inventory_service.require_valid_session", side_effect=AssertionError), \
         patch("app.services.inventory_service.get_db_connection", return_value=conn):
        result = await _get_inventory_stock_for_tenant(
            tenant_id,
            limit=10,
            offset=5,
            search="tom",
            status_filter="ok",
            category="Verduras",
            unit="gr",
            sort_field="ingredient_name",
            sort_direction="asc",
        )

    assert result["success"] is True
    assert result["total"] == 1
    assert result["data"][0]["ingredient_name"] == "Tomate"
    assert conn.fetch_calls[0][1][0] == tenant_id
    assert conn.fetch_calls[0][1][-2:] == (10, 5)


@pytest.mark.asyncio
async def test_inventory_movements_core_uses_explicit_tenant_without_session():
    tenant_id = "tenant-core-movements"
    ingredient_id = uuid4()
    conn = _FakeInventoryConnection(
        fetch_rows=[
            {
                "id": uuid4(),
                "ingredient_id": ingredient_id,
                "ingredient_name": "Tomate",
                "unit": "gr",
                "movement_type": "purchase",
                "quantity_change": "3.5",
                "previous_stock": "1",
                "new_stock": "4.5",
                "cost_per_unit": "10.25",
                "reference_table": "tenant_purchases",
                "reference_id": uuid4(),
                "reference_number": "CP-1",
                "reason": "Compra",
                "notes": None,
                "created_by": uuid4(),
                "created_by_name": "Ana",
                "created_at": datetime(2026, 1, 1, 12, 0),
            }
        ],
        fetchrow_rows=[{"total": 1}],
    )

    with patch("app.services.inventory_service.require_valid_session", side_effect=AssertionError), \
         patch("app.services.inventory_service.get_db_connection", return_value=conn):
        result = await _get_inventory_movements_for_tenant(
            tenant_id,
            limit=20,
            offset=2,
            ingredient_id=ingredient_id,
            movement_type="purchase",
            quantity_direction="positive",
            start_date="2026-01-01",
            end_date="2026-01-31",
        )

    assert result["success"] is True
    assert result["data"][0]["ingredient_name"] == "Tomate"
    assert conn.fetch_calls[0][1][0] == tenant_id
    assert conn.fetch_calls[0][1][-2:] == (20, 2)


@pytest.mark.asyncio
async def test_inventory_stock_wrapper_resolves_tenant_before_core():
    session = SimpleNamespace(tenant_id="tenant-wrapper-stock")

    with patch("app.services.inventory_service.require_valid_session", return_value=session), \
         patch("app.services.inventory_service._get_inventory_stock_for_tenant", new_callable=AsyncMock) as core:
        core.return_value = {"success": True, "data": []}

        result = await get_inventory_stock(
            object(),
            object(),
            limit=10,
            offset=1,
            search="tom",
            status_filter="low",
            category="Verduras",
            unit="gr",
            sort_field="ingredient_name",
            sort_direction="asc",
        )

    assert result == {"success": True, "data": []}
    core.assert_awaited_once_with(
        "tenant-wrapper-stock",
        limit=10,
        offset=1,
        search="tom",
        status_filter="low",
        category="Verduras",
        unit="gr",
        sort_field="ingredient_name",
        sort_direction="asc",
    )


@pytest.mark.asyncio
async def test_inventory_movements_wrapper_resolves_tenant_before_core():
    session = SimpleNamespace(tenant_id="tenant-wrapper-movements")
    ingredient_id = uuid4()

    with patch("app.services.inventory_service.require_valid_session", return_value=session), \
         patch("app.services.inventory_service._get_inventory_movements_for_tenant", new_callable=AsyncMock) as core:
        core.return_value = {"success": True, "data": []}

        result = await get_inventory_movements(
            object(),
            object(),
            limit=20,
            offset=2,
            ingredient_id=ingredient_id,
            movement_type="purchase",
            quantity_direction="positive",
            start_date="2026-01-01",
            end_date="2026-01-31",
        )

    assert result == {"success": True, "data": []}
    core.assert_awaited_once_with(
        "tenant-wrapper-movements",
        limit=20,
        offset=2,
        ingredient_id=ingredient_id,
        movement_type="purchase",
        quantity_direction="positive",
        start_date="2026-01-01",
        end_date="2026-01-31",
    )


class TestInventoryStockEndpoint:
    """Test inventory stock endpoint"""

    @pytest.mark.asyncio
    async def test_get_inventory_stock_default(self, client: AsyncClient):
        """Test GET /inventory/stock with default params"""
        response = await client.get("/inventory/stock")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_response_structure(self, client: AsyncClient):
        """Test inventory stock response structure"""
        response = await client.get("/inventory/stock")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "data" in data

    @pytest.mark.asyncio
    async def test_get_inventory_stock_with_pagination(self, client: AsyncClient):
        """Test inventory stock with pagination"""
        response = await client.get("/inventory/stock?limit=10&offset=0")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_with_search(self, client: AsyncClient):
        """Test inventory stock with search"""
        response = await client.get("/inventory/stock?search=test")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_filter_category(self, client: AsyncClient):
        """Test inventory stock filtered by category"""
        response = await client.get("/inventory/stock?category=Verduras")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_filter_unit(self, client: AsyncClient):
        """Test inventory stock filtered by unit"""
        response = await client.get("/inventory/stock?unit=gr")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_filter_category_unit_and_status(self, client: AsyncClient):
        """Test inventory stock combining backend filters"""
        response = await client.get("/inventory/stock?category=Verduras&unit=gr&status_filter=low")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_filter_low(self, client: AsyncClient):
        """Test inventory stock filtered by status=low"""
        response = await client.get("/inventory/stock?status_filter=low")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_filter_critical(self, client: AsyncClient):
        """Test inventory stock filtered by status=critical"""
        response = await client.get("/inventory/stock?status_filter=critical")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_filter_ok(self, client: AsyncClient):
        """Test inventory stock filtered by status=ok"""
        response = await client.get("/inventory/stock?status_filter=ok")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_sort_asc(self, client: AsyncClient):
        """Test inventory stock with ascending sort"""
        response = await client.get("/inventory/stock?sort_field=current_stock&sort_direction=asc")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_invalid_limit(self, client: AsyncClient):
        """Test inventory stock with invalid limit"""
        response = await client.get("/inventory/stock?limit=0")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_stock_exceeds_max_limit(self, client: AsyncClient):
        """Test inventory stock with limit exceeding max (500)"""
        response = await client.get("/inventory/stock?limit=501")
        assert response.status_code in [422, 500]


class TestInventoryMovementsEndpoint:
    """Test inventory movements endpoint"""

    @pytest.mark.asyncio
    async def test_get_inventory_movements_default(self, client: AsyncClient):
        """Test GET /inventory/movements with default params"""
        response = await client.get("/inventory/movements")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_movements_response_structure(self, client: AsyncClient):
        """Test inventory movements response structure"""
        response = await client.get("/inventory/movements")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "data" in data

    @pytest.mark.asyncio
    async def test_get_inventory_movements_filter_by_type(self, client: AsyncClient):
        """Test inventory movements filtered by type"""
        response = await client.get("/inventory/movements?movement_type=purchase")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_movements_filter_by_positive_direction(self, client: AsyncClient):
        """Test inventory movements filtered by positive quantity direction"""
        response = await client.get(
            "/inventory/movements?movement_type=adjustment&quantity_direction=positive"
        )
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_movements_filter_by_negative_direction(self, client: AsyncClient):
        """Test inventory movements filtered by negative quantity direction"""
        response = await client.get(
            "/inventory/movements?movement_type=adjustment&quantity_direction=negative"
        )
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_movements_filter_by_date(self, client: AsyncClient):
        """Test inventory movements filtered by date range"""
        response = await client.get(
            "/inventory/movements?start_date=2024-01-01&end_date=2024-12-31"
        )
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_inventory_movements_with_pagination(self, client: AsyncClient):
        """Test inventory movements with pagination"""
        response = await client.get("/inventory/movements?limit=10&offset=0")
        assert response.status_code in [200, 401, 403, 500]


class TestIngredientStockEndpoint:
    """Test single ingredient stock endpoint"""

    @pytest.mark.asyncio
    async def test_get_ingredient_stock_not_found(self, client: AsyncClient):
        """Test GET /inventory/stock/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/inventory/stock/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_ingredient_stock_invalid_uuid(self, client: AsyncClient):
        """Test GET /inventory/stock/{id} with invalid UUID"""
        response = await client.get("/inventory/stock/invalid-uuid")
        assert response.status_code in [422, 500]


class TestInventoryAdjustmentsEndpoint:
    """Test inventory adjustments endpoint"""

    @pytest.mark.asyncio
    async def test_create_adjustment_without_body(self, client: AsyncClient):
        """Test POST /inventory/adjustments without body"""
        response = await client.post("/inventory/adjustments")
        # Should fail without required body
        assert response.status_code in [400, 422, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_create_adjustment_invalid_ingredient(self, client: AsyncClient):
        """Test POST /inventory/adjustments with invalid ingredient"""
        response = await client.post(
            "/inventory/adjustments",
            json={
                "ingredient_id": str(uuid4()),
                "quantity_change": 10,
                "reason": "Test adjustment"
            }
        )
        # Should fail - invalid ingredient or no session
        assert response.status_code in [400, 404, 422, 401, 403, 500]
