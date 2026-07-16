from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.models.modifier import ModifierCreate
from app.services.modifier_option_service import (
    modifier_chargeable_quantity,
    modifier_line_subtotal,
    resolve_modifier_selections,
)


class _ResolverConn:
    def __init__(self, group, modifier):
        self.group = group
        self.modifier = modifier

    async def fetch(self, query, *args):
        if "FROM product_modifier_groups" in query:
            return [self.group]
        if "FROM modifiers m" in query:
            return [self.modifier]
        raise AssertionError(query)


@pytest.mark.parametrize(
    ("selected", "expected"),
    [(1, Decimal("0")), (2, Decimal("2000")), (3, Decimal("4000"))],
)
def test_excess_pricing_with_one_included_unit(selected, expected):
    assert modifier_line_subtotal(2000, selected, 1) == expected


def test_chargeable_quantity_never_goes_negative():
    assert modifier_chargeable_quantity(0, 1) == Decimal("0")
    assert modifier_line_subtotal(0, 10, 2) == Decimal("0")


def test_modifier_create_defaults_included_quantity_to_zero():
    modifier = ModifierCreate(
        name="Salsa",
        price=2000,
        max_limit=3,
        option_type="NONE",
    )
    assert modifier.included_quantity == 0
    assert "included_quantity" not in modifier.model_fields_set


@pytest.mark.asyncio
async def test_resolver_uses_persisted_price_name_and_included_quantity():
    group_id = uuid4()
    modifier_id = uuid4()
    product_id = uuid4()
    conn = _ResolverConn(
        {
            "id": group_id,
            "name": "Salsas",
            "is_required": False,
            "min_qty": 0,
            "max_qty": 2,
        },
        {
            "id": modifier_id,
            "modifier_group_id": group_id,
            "name": "Salsa real",
            "price": Decimal("2000"),
            "max_limit": 3,
            "included_quantity": 1,
            "is_available": True,
        },
    )

    resolved = await resolve_modifier_selections(
        conn,
        product_id,
        [{"id": modifier_id, "name": "Manipulado", "price": 1, "quantity": 3}],
    )

    assert resolved == [
        {
            "id": modifier_id,
            "name": "Salsa real",
            "price": Decimal("2000"),
            "quantity": 3,
            "included_quantity": 1,
            "chargeable_quantity": 2,
            "subtotal": Decimal("4000"),
        }
    ]


@pytest.mark.asyncio
async def test_resolver_rejects_quantity_above_modifier_limit():
    group_id = uuid4()
    modifier_id = uuid4()
    conn = _ResolverConn(
        {
            "id": group_id,
            "name": "Salsas",
            "is_required": False,
            "min_qty": 0,
            "max_qty": 2,
        },
        {
            "id": modifier_id,
            "modifier_group_id": group_id,
            "name": "Salsa",
            "price": Decimal("2000"),
            "max_limit": 2,
            "included_quantity": 1,
            "is_available": True,
        },
    )

    with pytest.raises(APIError) as exc_info:
        await resolve_modifier_selections(
            conn, uuid4(), [{"id": modifier_id, "quantity": 3}]
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_resolver_rejects_modifier_from_another_product():
    selected_group_id = uuid4()
    conn = _ResolverConn(
        {
            "id": uuid4(),
            "name": "Valid group",
            "is_required": False,
            "min_qty": 0,
            "max_qty": 2,
        },
        {
            "id": uuid4(),
            "modifier_group_id": selected_group_id,
            "name": "Foreign",
            "price": Decimal("2000"),
            "max_limit": 2,
            "included_quantity": 0,
            "is_available": True,
        },
    )

    with pytest.raises(APIError) as exc_info:
        await resolve_modifier_selections(
            conn, uuid4(), [{"id": conn.modifier["id"], "quantity": 1}]
        )
    assert exc_info.value.status_code == 422
