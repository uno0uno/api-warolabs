"""Cart item promo opt-out persistence (warocol.com#1003)."""
from uuid import uuid4

from app.services.pos_cart_service import _cart_items_to_promo_lines
from app.services.promotions_service import evaluate_cart_promotions


def _promo(*, product_id):
    return {
        "id": uuid4(),
        "name": "10% off",
        "promo_type": "percent_off",
        "value_json": {"percent": 10},
        "scope_type": "products",
        "priority": 10,
        "stackable": False,
        "category_ids": set(),
        "product_ids": {product_id},
    }


def test_cart_items_to_promo_lines_carries_opt_out_flag():
    product_id = str(uuid4())
    items = [
        {
            "id": str(uuid4()),
            "product_id": product_id,
            "category_id": None,
            "quantity": 1,
            "subtotal": 10000.0,
            "tax_category": "standard",
            "promo_opt_out": True,
            "product": {"id": product_id},
        }
    ]
    promo_lines = _cart_items_to_promo_lines(items)
    assert promo_lines[0]["promo_opt_out"] is True


def test_cart_items_to_promo_lines_excludes_optional_modifier_basis():
    product_id = str(uuid4())
    item_id = str(uuid4())
    items = [{
        "id": item_id,
        "product_id": product_id,
        "category_id": None,
        "quantity": 2,
        "subtotal": 31000.0,
        "tax_category": "standard",
        "promo_opt_out": False,
        "product": {"id": product_id, "price": 10000.0},
        "modifiers": [
            {"id": str(uuid4()), "price": 2000.0, "group_is_required": True},
            {"id": str(uuid4()), "price": 500.0, "is_default": True},
            {"id": str(uuid4()), "price": 3000.0},
        ],
    }]

    promo_lines = _cart_items_to_promo_lines(items)

    assert promo_lines[0]["subtotal"] == 31000.0
    assert promo_lines[0]["promo_eligible_subtotal"] == 25000.0


def test_evaluate_from_cart_items_respects_opt_out():
    product_id = uuid4()
    line_id = str(uuid4())
    items = [
        {
            "id": line_id,
            "product_id": str(product_id),
            "category_id": None,
            "quantity": 1,
            "subtotal": 10000.0,
            "tax_category": "standard",
            "promo_opt_out": True,
            "product": {"id": str(product_id)},
        }
    ]
    promo_lines = _cart_items_to_promo_lines(items)
    result = evaluate_cart_promotions(promo_lines, [_promo(product_id=product_id)])
    assert result["lines"][0]["id"] == line_id
    assert result["lines"][0]["promo_savings"] == 0
    assert result["promo_savings"] == 0
