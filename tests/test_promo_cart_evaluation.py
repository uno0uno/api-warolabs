"""POS cart promotion evaluation (warocol.com#982)."""
from uuid import uuid4

from app.services.promotions_service import (
    apply_manual_discount_to_evaluated_lines,
    evaluate_cart_promotions,
)


def _line(*, subtotal: float, quantity: int = 1, product_id=None, category_id=None):
    return {
        "id": str(uuid4()),
        "product_id": str(product_id or uuid4()),
        "category_id": str(category_id) if category_id else None,
        "quantity": quantity,
        "subtotal": subtotal,
    }


def _promo(
    *,
    promo_type: str,
    value_json: dict,
    scope_type: str = "all_products",
    priority: int = 10,
    name: str = "Test promo",
):
    return {
        "id": uuid4(),
        "name": name,
        "promo_type": promo_type,
        "value_json": value_json,
        "scope_type": scope_type,
        "priority": priority,
        "stackable": False,
        "category_ids": set(),
        "product_ids": set(),
    }


def test_bogo_2x1_on_three_units():
    product_id = uuid4()
    lines = [_line(subtotal=30000, quantity=3, product_id=product_id)]
    promos = [_promo(promo_type="bogo", value_json={"buy_qty": 1, "get_qty": 1})]
    result = evaluate_cart_promotions(lines, promos)
    assert result["promo_savings"] == 10000
    assert result["subtotal_after_promos"] == 20000
    assert result["lines"][0]["promotion_name"] == "Test promo"


def test_percent_off_scoped_product():
    product_id = uuid4()
    other_id = uuid4()
    promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 10},
        scope_type="products",
    )
    promo["product_ids"] = {product_id}
    lines = [
        _line(subtotal=10000, product_id=product_id),
        _line(subtotal=5000, product_id=other_id),
    ]
    result = evaluate_cart_promotions(lines, promos=[promo])
    assert result["promo_savings"] == 1000
    assert result["subtotal_after_promos"] == 14000


def test_manual_discount_stacks_after_promo():
    lines = [_line(subtotal=10000, quantity=1)]
    promos = [_promo(promo_type="percent_off", value_json={"percent": 10})]
    evaluated = evaluate_cart_promotions(lines, promos)
    checkout = apply_manual_discount_to_evaluated_lines(evaluated, 1000)
    assert checkout["promo_savings"] == 1000
    assert checkout["manual_discount_amount"] == 1000
    assert checkout["total_amount"] == 8000


def test_ineligible_lines_unchanged():
    lines = [_line(subtotal=8000, quantity=2)]
    result = evaluate_cart_promotions(lines, promos=[])
    assert result["promo_savings"] == 0
    assert result["subtotal_after_promos"] == 8000
