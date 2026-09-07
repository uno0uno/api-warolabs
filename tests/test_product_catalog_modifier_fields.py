from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from app.models.product import Modifier, ProductsListResponse


def test_product_modifier_response_keeps_included_quantity_and_option_type():
    modifier_id = uuid4()
    group_id = uuid4()
    product_id = uuid4()
    tenant_id = uuid4()
    category_id = uuid4()
    now = datetime(2026, 7, 21, 12, 0, 0)

    response = ProductsListResponse(
        success=True,
        total=1,
        data=[{
            "id": product_id,
            "tenant_id": tenant_id,
            "category_id": category_id,
            "name": "BUBASICO TRADICIONAL",
            "price": Decimal("14500"),
            "created_at": now,
            "updated_at": now,
            "modifier_groups": [{
                "id": group_id,
                "name": "SABORES DE HELADOS TRADICIONALES",
                "min_qty": 1,
                "max_qty": 2,
                "is_required": True,
                "modifiers": [{
                    "id": modifier_id,
                    "name": "H. AREQUIPE",
                    "price": Decimal("6000"),
                    "max_limit": 2,
                    "included_quantity": 1,
                    "option_type": "INGREDIENT",
                    "is_available": True,
                }],
            }],
        }],
    )

    payload = response.model_dump(mode="json")
    modifier = payload["data"][0]["modifier_groups"][0]["modifiers"][0]
    assert modifier["included_quantity"] == 1
    assert modifier["option_type"] == "INGREDIENT"
    assert modifier["price"] == "6000"


def test_product_modifier_defaults_included_quantity_to_zero():
    parsed = Modifier.model_validate({
        "id": uuid4(),
        "name": "Salsa",
        "price": Decimal("0"),
        "max_limit": 1,
    })
    assert parsed.included_quantity == 0
    assert parsed.option_type == "INGREDIENT"
