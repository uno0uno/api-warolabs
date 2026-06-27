from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import public_restaurant as public_restaurant_router
from app.services import public_restaurant_service


def _mock_db(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
async def test_list_cities_include_empty_returns_catalog_metadata():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "country": "Colombia",
            "city": "Bogotá",
            "city_slug": "bogota",
            "department_code": "11",
            "department_name": "Bogotá, D.C.",
            "municipality_code": "11001",
            "municipality_type": "Municipio",
            "latitude": 4.649251,
            "longitude": -74.106992,
            "sort_order": 10,
            "tenant_count": 0,
        },
        {
            "country": "Colombia",
            "city": "Mosquera",
            "city_slug": "mosquera",
            "department_code": "25",
            "department_name": "Cundinamarca",
            "municipality_code": "25473",
            "municipality_type": "Municipio",
            "latitude": 4.706530,
            "longitude": -74.221154,
            "sort_order": 90,
            "tenant_count": 2,
        },
    ])

    with patch(
        "app.services.public_restaurant_service.get_db_connection",
        return_value=_mock_db(conn),
    ):
        result = await public_restaurant_service.list_cities(include_empty=True)

    assert [city["city_slug"] for city in result] == ["bogota", "mosquera"]
    assert result[0]["municipality_code"] == "11001"
    assert result[0]["department_name"] == "Bogotá, D.C."
    assert result[0]["tenant_count"] == 0


@pytest.mark.asyncio
async def test_list_cities_default_hides_empty_catalog_rows():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "country": "Colombia",
            "city": "La Unión",
            "city_slug": "la-union-antioquia",
            "department_code": "05",
            "department_name": "Antioquia",
            "municipality_code": "05400",
            "municipality_type": "Municipio",
            "latitude": 5.973845,
            "longitude": -75.360874,
            "sort_order": 6400,
            "tenant_count": 0,
        },
        {
            "country": "Colombia",
            "city": "Mosquera",
            "city_slug": "mosquera",
            "department_code": "25",
            "department_name": "Cundinamarca",
            "municipality_code": "25473",
            "municipality_type": "Municipio",
            "latitude": 4.706530,
            "longitude": -74.221154,
            "sort_order": 90,
            "tenant_count": 1,
        },
    ])

    with patch(
        "app.services.public_restaurant_service.get_db_connection",
        return_value=_mock_db(conn),
    ):
        result = await public_restaurant_service.list_cities()

    assert [city["city_slug"] for city in result] == ["mosquera"]


@pytest.mark.asyncio
async def test_is_city_slug_known_checks_active_catalog():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[1, None])

    with patch(
        "app.services.public_restaurant_service.get_db_connection",
        return_value=_mock_db(conn),
    ):
        assert await public_restaurant_service.is_city_slug_known("la-union-antioquia")
        assert not await public_restaurant_service.is_city_slug_known("no-existe")

    assert conn.fetchval.await_count == 2


def test_cities_endpoint_passes_include_empty_flag():
    app = FastAPI()
    app.include_router(public_restaurant_router.router, prefix="/public/restaurant")

    payload = [{
        "country": "Colombia",
        "city": "Mosquera",
        "city_slug": "mosquera",
        "department_code": "25",
        "department_name": "Cundinamarca",
        "municipality_code": "25473",
        "municipality_type": "Municipio",
        "tenant_count": 0,
    }]

    with patch(
        "app.routers.public_restaurant.public_restaurant_service.list_cities",
        new=AsyncMock(return_value=payload),
    ) as list_cities:
        client = TestClient(app)
        response = client.get("/public/restaurant/cities?include_empty=true")

    assert response.status_code == 200
    assert response.json()["data"][0]["municipality_code"] == "25473"
    list_cities.assert_awaited_once_with(include_empty=True)
