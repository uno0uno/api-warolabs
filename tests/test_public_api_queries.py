from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request

from app.routers import public_api


def _request():
    return Request({"type": "http"})


def test_public_api_registers_queries_routes():
    paths = {route.path for route in public_api.router.routes}

    assert "/v1/queries/schema" in paths
    assert "/v1/queries/run" in paths


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
