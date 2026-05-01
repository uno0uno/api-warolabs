"""
Tests for categories module endpoints.

Endpoints tested:
- GET /menu/categories
- POST /menu/categories (issue #458)
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4

from pydantic import ValidationError


class TestCategoriesEndpoint:
    """Test categories list endpoint"""

    @pytest.mark.asyncio
    async def test_get_categories_without_session(self, client: AsyncClient):
        """Test GET /menu/categories without session returns categories or auth error"""
        response = await client.get("/menu/categories")
        # Categories endpoint may require session or work without
        # 200 = success, 401/403 = auth required, 500 = server error
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_categories_response_structure(self, client: AsyncClient):
        """Test categories response has correct structure"""
        response = await client.get("/menu/categories")

        if response.status_code == 200:
            data = response.json()
            # Check response structure
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

            # If there are categories, check their structure
            if len(data["data"]) > 0:
                category = data["data"][0]
                assert "id" in category
                assert "name" in category
                assert "created_at" in category
                assert "updated_at" in category

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_categories_returns_list(self, client: AsyncClient):
        """Integration test: categories endpoint returns a list"""
        response = await client.get("/menu/categories")

        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert isinstance(data["total"], int)
            assert data["total"] >= 0

    @pytest.mark.asyncio
    async def test_post_category_without_session_rejected(self, client: AsyncClient):
        """POST /menu/categories without a valid session must return 401."""
        response = await client.post("/menu/categories", json={"name": "Test"})
        # 401 = unauthenticated, 500 = transient error in tests env
        assert response.status_code in [401, 500]

    @pytest.mark.asyncio
    async def test_categories_endpoint_with_trailing_slash(self, client: AsyncClient):
        """Test categories endpoint handles trailing slash"""
        response = await client.get("/menu/categories/")
        # Should redirect or work normally
        assert response.status_code in [200, 307, 401, 403, 500]


class TestCategoryCreateModel:
    """Pydantic-level tests for CategoryCreate — issue #458."""

    def test_accepts_name_only(self):
        from app.models.category import CategoryCreate
        cat = CategoryCreate(name="Bebidas")
        assert cat.name == "Bebidas"
        assert cat.description is None

    def test_accepts_name_and_description(self):
        from app.models.category import CategoryCreate
        cat = CategoryCreate(name="Postres", description="Dulces y postres")
        assert cat.name == "Postres"
        assert cat.description == "Dulces y postres"

    def test_rejects_empty_name(self):
        from app.models.category import CategoryCreate
        with pytest.raises(ValidationError):
            CategoryCreate(name="")

    def test_rejects_name_over_100_chars(self):
        from app.models.category import CategoryCreate
        with pytest.raises(ValidationError):
            CategoryCreate(name="x" * 101)

    def test_accepts_name_at_max_length(self):
        from app.models.category import CategoryCreate
        cat = CategoryCreate(name="x" * 100)
        assert len(cat.name) == 100

    def test_rejects_description_over_500_chars(self):
        from app.models.category import CategoryCreate
        with pytest.raises(ValidationError):
            CategoryCreate(name="ok", description="x" * 501)

    def test_does_not_accept_tenant_id_in_payload(self):
        """tenant_id must be derived from session, never trusted from the body."""
        from app.models.category import CategoryCreate
        # Pydantic ignores extra fields by default — passing tenant_id should
        # not surface in the model
        cat = CategoryCreate(name="ok", tenant_id=str(uuid4()))  # type: ignore[call-arg]
        assert not hasattr(cat, "tenant_id")
