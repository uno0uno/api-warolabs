"""Settlement math for split payments + tips (warocol.com#737)."""
from app.services.pos_cart_service import _split_settlement_amount_due


def test_split_settlement_amount_due_includes_tip():
    assert _split_settlement_amount_due(100_000, 10_000) == 110_000


def test_split_settlement_amount_due_zero_tip():
    assert _split_settlement_amount_due(50_000, 0) == 50_000


def test_split_settlement_amount_due_none_tip_treated_as_zero():
    assert _split_settlement_amount_due(50_000, None) == 50_000
