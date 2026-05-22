"""Open-priced (venta libre) unit price validation — warocol.com#795."""
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.services import open_priced_service


def _pricing(catalog: str, open_priced: bool = False):
    pid = uuid4()
    return (
        pid,
        {
            str(pid): {
                "price": Decimal(catalog),
                "open_priced": open_priced,
            }
        },
    )


def test_open_priced_accepts_custom_unit_price():
    product_id, pricing = _pricing("10000", open_priced=True)
    resolved = open_priced_service.resolve_line_unit_price(
        pricing, product_id, 25000, []
    )
    assert resolved == Decimal("25000.00")


def test_open_priced_rejects_zero_price():
    product_id, pricing = _pricing("1", open_priced=True)
    with pytest.raises(ValidationError):
        open_priced_service.resolve_line_unit_price(pricing, product_id, 0, [])


def test_open_priced_rejects_modifiers():
    product_id, pricing = _pricing("1", open_priced=True)
    with pytest.raises(ValidationError):
        open_priced_service.resolve_line_unit_price(
            pricing,
            product_id,
            5000,
            [{"id": None, "name": "Extra", "price": 1}],
        )


def test_catalog_product_requires_matching_price():
    product_id, pricing = _pricing("15000.00")
    resolved = open_priced_service.resolve_line_unit_price(
        pricing, product_id, 15000, []
    )
    assert resolved == Decimal("15000.00")


def test_catalog_product_rejects_price_mismatch():
    product_id, pricing = _pricing("15000")
    with pytest.raises(ValidationError):
        open_priced_service.resolve_line_unit_price(pricing, product_id, 20000, [])


def test_catalog_product_allows_small_rounding_tolerance():
    product_id, pricing = _pricing("10.00")
    resolved = open_priced_service.resolve_line_unit_price(
        pricing, product_id, 10.009, []
    )
    assert resolved == Decimal("10.00")


def test_missing_product_raises_not_found():
    product_id = uuid4()
    with pytest.raises(NotFoundError):
        open_priced_service.resolve_line_unit_price({}, product_id, 10, [])


def test_validate_items_normalizes_unit_price_in_place():
    pid, pricing = _pricing("12000")
    items = [{"product_id": pid, "quantity": 1, "unit_price": 12000.0}]
    open_priced_service.validate_items_unit_prices(pricing, items)
    assert items[0]["unit_price"] == 12000.0
