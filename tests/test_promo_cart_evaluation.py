"""POS cart promotion evaluation (warocol.com#982)."""
from uuid import uuid4

from app.services.promotions_service import (
    apply_manual_discount_to_evaluated_lines,
    evaluate_cart_promotions,
)


def _line(
    *,
    subtotal: float,
    quantity: int = 1,
    product_id=None,
    category_id=None,
    promo_eligible_subtotal=None,
):
    line = {
        "id": str(uuid4()),
        "product_id": str(product_id or uuid4()),
        "category_id": str(category_id) if category_id else None,
        "quantity": quantity,
        "subtotal": subtotal,
    }
    if promo_eligible_subtotal is not None:
        line["promo_eligible_subtotal"] = promo_eligible_subtotal
    return line


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
    result = evaluate_cart_promotions(lines, [promo])
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
    result = evaluate_cart_promotions(lines, [])
    assert result["promo_savings"] == 0
    assert result["subtotal_after_promos"] == 8000


def test_locked_line_keeps_promo_when_current_promos_empty():
    promo_id = uuid4()
    lines = [
        {
            **_line(subtotal=10000),
            "locked_promotion_id": str(promo_id),
            "locked_promotion_name": "Happy hour",
            "locked_promo_type": "percent_off",
            "locked_promo_savings": 1000,
        },
        _line(subtotal=5000),
    ]
    result = evaluate_cart_promotions(lines, [])
    assert result["promo_savings"] == 1000
    assert result["subtotal_after_promos"] == 14000
    assert result["lines"][0]["promotion_id"] == str(promo_id)
    assert result["lines"][0]["promotion_name"] == "Happy hour"
    assert result["lines"][1]["promo_savings"] == 0


def test_locked_line_opt_out_ignores_snapshot():
    lines = [{
        **_line(subtotal=10000),
        "promo_opt_out": True,
        "locked_promotion_id": str(uuid4()),
        "locked_promotion_name": "Happy hour",
        "locked_promo_type": "percent_off",
        "locked_promo_savings": 1000,
    }]
    result = evaluate_cart_promotions(lines, [])
    assert result["promo_savings"] == 0
    assert result["lines"][0].get("promotion_id") is None


def test_percent_off_category_scoped():
    """Happy hour % off all items in a category (warocol.com#983)."""
    category_id = uuid4()
    other_category = uuid4()
    promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 10},
        scope_type="categories",
        name="Happy hour Cervezas",
    )
    promo["category_ids"] = {category_id}
    lines = [
        _line(subtotal=10000, category_id=category_id),
        _line(subtotal=5000, category_id=other_category),
    ]
    result = evaluate_cart_promotions(lines, [promo])
    assert result["promo_savings"] == 1000
    assert result["subtotal_after_promos"] == 14000
    assert result["lines"][0]["promotion_name"] == "Happy hour Cervezas"
    assert result["lines"][1]["promo_savings"] == 0


def test_percent_off_category_large_menu():
    """Category-wide rule applies across many SKUs (warocol.com#983)."""
    category_id = uuid4()
    promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 20},
        scope_type="categories",
    )
    promo["category_ids"] = {category_id}
    lines = [
        _line(subtotal=1000, category_id=category_id) for _ in range(150)
    ]
    result = evaluate_cart_promotions(lines, [promo])
    assert result["promo_savings"] == 30000
    assert result["subtotal_after_promos"] == 120000


def test_promo_opt_out_skips_line_promotion():
    """Opted-out lines keep gross subtotal (warocol.com#1003)."""
    product_id = uuid4()
    promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 10},
        scope_type="products",
    )
    promo["product_ids"] = {product_id}
    lines = [
        {**_line(subtotal=10000, product_id=product_id), "promo_opt_out": True},
        _line(subtotal=5000, product_id=product_id),
    ]
    result = evaluate_cart_promotions(lines, [promo])
    assert result["lines"][0]["promo_savings"] == 0
    assert result["lines"][0].get("promotion_name") is None
    assert result["lines"][1]["promo_savings"] == 500
    assert result["promo_savings"] == 500
    assert result["subtotal_after_promos"] == 14500


def test_manual_discount_after_promo_opt_out():
    """Manual discount applies on promo-adjusted subtotal when a line opts out."""
    product_id = uuid4()
    promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 10},
        scope_type="products",
    )
    promo["product_ids"] = {product_id}
    lines = [
        {**_line(subtotal=10000, product_id=product_id), "promo_opt_out": True},
        _line(subtotal=10000, product_id=product_id),
    ]
    evaluated = evaluate_cart_promotions(lines, [promo])
    checkout = apply_manual_discount_to_evaluated_lines(evaluated, 900)
    assert checkout["promo_savings"] == 1000
    assert checkout["subtotal_after_promos"] == 19000
    assert checkout["manual_discount_amount"] == 900
    assert checkout["total_amount"] == 18100


def test_three_promo_types_one_winner_per_line():
    """BOGO + percent + fixed on one SKU — exactly one promo applies (warocol.com#1012)."""
    product_id = uuid4()
    lines = [_line(subtotal=20000, quantity=2, product_id=product_id)]
    bogo = _promo(
        promo_type="bogo",
        value_json={"buy_qty": 1, "get_qty": 1},
        name="BOGO deal",
        priority=10,
    )
    percent = _promo(
        promo_type="percent_off",
        value_json={"percent": 30},
        name="Percent deal",
        priority=10,
    )
    fixed = _promo(
        promo_type="fixed_off",
        value_json={"amount_cop": 5000},
        name="Fixed deal",
        priority=10,
    )
    result = evaluate_cart_promotions(lines, [percent, fixed, bogo])
    assert result["lines"][0]["promotion_name"] == "BOGO deal"
    assert result["promo_savings"] == 10000


def test_bogo_blocks_percent_and_fixed_at_equal_priority():
    product_id = uuid4()
    lines = [_line(subtotal=20000, quantity=2, product_id=product_id)]
    bogo = _promo(
        promo_type="bogo",
        value_json={"buy_qty": 1, "get_qty": 1},
        name="BOGO",
        priority=5,
    )
    percent = _promo(
        promo_type="percent_off",
        value_json={"percent": 50},
        name="Half off",
        priority=5,
    )
    result = evaluate_cart_promotions(lines, [bogo, percent])
    assert result["lines"][0]["promotion_name"] == "BOGO"
    assert result["promo_savings"] == 10000


def test_higher_priority_percent_wins_over_bogo_block():
    product_id = uuid4()
    lines = [_line(subtotal=10000, quantity=1, product_id=product_id)]
    bogo = _promo(
        promo_type="bogo",
        value_json={"buy_qty": 1, "get_qty": 1},
        name="BOGO",
        priority=0,
    )
    percent = _promo(
        promo_type="percent_off",
        value_json={"percent": 30},
        name="VIP percent",
        priority=20,
    )
    result = evaluate_cart_promotions(lines, [bogo, percent])
    assert result["lines"][0]["promotion_name"] == "VIP percent"
    assert result["promo_savings"] == 3000


def test_product_scope_beats_category_at_equal_priority():
    product_id = uuid4()
    category_id = uuid4()
    lines = [_line(subtotal=10000, quantity=1, product_id=product_id, category_id=category_id)]
    category_promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 50},
        scope_type="categories",
        name="Category wide",
        priority=10,
    )
    category_promo["category_ids"] = {category_id}
    product_promo = _promo(
        promo_type="percent_off",
        value_json={"percent": 10},
        scope_type="products",
        name="Product specific",
        priority=10,
    )
    product_promo["product_ids"] = {product_id}
    result = evaluate_cart_promotions(lines, [category_promo, product_promo])
    assert result["lines"][0]["promotion_name"] == "Product specific"
    assert result["promo_savings"] == 1000


def test_bogo_cross_line_cheapest_first():
    """Sibling qty=1 lines same product — free units hit cheapest line (warocol.com#1023)."""
    product_id = uuid4()
    promos = [_promo(promo_type="bogo", value_json={"buy_qty": 2, "get_qty": 1}, name="3x1")]
    lines = [
        _line(subtotal=15000, quantity=1, product_id=product_id),
        _line(subtotal=12000, quantity=1, product_id=product_id),
        _line(subtotal=10000, quantity=1, product_id=product_id),
    ]
    result = evaluate_cart_promotions(lines, promos)
    assert result["promo_savings"] == 10000
    assert result["subtotal_after_promos"] == 27000
    by_subtotal = {line["subtotal"]: line["promo_savings"] for line in result["lines"]}
    assert by_subtotal[10000] == 10000
    assert by_subtotal[12000] == 0
    assert by_subtotal[15000] == 0


def test_percent_off_uses_promo_eligible_subtotal():
    """Optional modifier extras stay charged outside percent promos (api-warolabs#422)."""
    lines = [_line(subtotal=12000, promo_eligible_subtotal=10000)]
    promo = _promo(promo_type="percent_off", value_json={"percent": 10})

    result = evaluate_cart_promotions(lines, [promo])

    assert result["promo_savings"] == 1000
    assert result["subtotal_after_promos"] == 11000
    assert result["lines"][0]["subtotal_after_promo"] == 11000


def test_fixed_off_caps_at_promo_eligible_subtotal():
    """Fixed promos cannot consume optional modifier extras (api-warolabs#422)."""
    lines = [_line(subtotal=14000, promo_eligible_subtotal=9000)]
    promo = _promo(promo_type="fixed_off", value_json={"amount_cop": 12000})

    result = evaluate_cart_promotions(lines, [promo])

    assert result["promo_savings"] == 9000
    assert result["subtotal_after_promos"] == 5000


def test_same_line_bogo_uses_eligible_unit_price():
    """BOGO free units use eligible unit price, not gross unit with optional extras."""
    product_id = uuid4()
    lines = [_line(
        subtotal=24000,
        quantity=2,
        product_id=product_id,
        promo_eligible_subtotal=20000,
    )]
    promo = _promo(promo_type="bogo", value_json={"buy_qty": 1, "get_qty": 1})

    result = evaluate_cart_promotions(lines, [promo])

    assert result["promo_savings"] == 10000
    assert result["subtotal_after_promos"] == 14000


def test_cross_line_bogo_uses_eligible_unit_price_and_cap():
    """Cross-line BOGO allocation ignores optional extras when choosing free units."""
    product_id = uuid4()
    promo = _promo(promo_type="bogo", value_json={"buy_qty": 2, "get_qty": 1}, name="3x1")
    lines = [
        _line(subtotal=20000, quantity=1, product_id=product_id, promo_eligible_subtotal=10000),
        _line(subtotal=12000, quantity=1, product_id=product_id, promo_eligible_subtotal=12000),
        _line(subtotal=11000, quantity=1, product_id=product_id, promo_eligible_subtotal=11000),
    ]

    result = evaluate_cart_promotions(lines, [promo])

    assert result["promo_savings"] == 10000
    by_subtotal = {line["subtotal"]: line["promo_savings"] for line in result["lines"]}
    assert by_subtotal[20000] == 10000
    assert by_subtotal[12000] == 0
    assert by_subtotal[11000] == 0


def _locked_bogo_line(*, promo, subtotal, quantity, locked_savings, product_id):
    return {
        **_line(subtotal=subtotal, quantity=quantity, product_id=product_id),
        "locked_promotion_id": str(promo["id"]),
        "locked_promotion_name": promo["name"],
        "locked_promo_type": "bogo",
        "locked_promo_savings": locked_savings,
    }


def test_locked_bogo_recalibrates_when_pool_grows():
    """#665: 4+2 units in two tab rounds with BOGO 1+1 must grant 3 free units,
    not truncate to the first round's locked savings (44000)."""
    product_id = uuid4()
    promo = _promo(promo_type="bogo", value_json={"buy_qty": 1, "get_qty": 1})
    lines = [
        _locked_bogo_line(
            promo=promo, subtotal=88000, quantity=4,
            locked_savings=44000, product_id=product_id,
        ),
        _line(subtotal=44000, quantity=2, product_id=product_id),
    ]
    result = evaluate_cart_promotions(lines, [promo])
    assert result["promo_savings"] == 66000
    assert result["subtotal_after_promos"] == 66000


def test_locked_bogo_recalibrates_when_pool_shrinks():
    """#665: a stale lock from a larger pool must shrink back when the tab now
    only holds 4 eligible units (2 free)."""
    product_id = uuid4()
    promo = _promo(promo_type="bogo", value_json={"buy_qty": 1, "get_qty": 1})
    lines = [
        _locked_bogo_line(
            promo=promo, subtotal=88000, quantity=4,
            locked_savings=66000, product_id=product_id,
        ),
    ]
    result = evaluate_cart_promotions(lines, [promo])
    assert result["promo_savings"] == 44000
    assert result["subtotal_after_promos"] == 44000


def test_locked_bogo_kept_when_promo_inactive():
    """The snapshot lock still protects savings when the BOGO is no longer
    evaluable (deactivated/expired) — its original purpose (#4484b6d)."""
    product_id = uuid4()
    promo = _promo(promo_type="bogo", value_json={"buy_qty": 1, "get_qty": 1})
    lines = [
        _locked_bogo_line(
            promo=promo, subtotal=88000, quantity=4,
            locked_savings=44000, product_id=product_id,
        ),
    ]
    result = evaluate_cart_promotions(lines, [])
    assert result["promo_savings"] == 44000
    assert result["lines"][0]["promotion_id"] == str(promo["id"])
    assert result["lines"][0]["promotion_name"] == "Test promo"
