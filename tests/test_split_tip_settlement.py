"""Settlement math for split payments + tips (warocol.com#737, #740)."""
from app.services.tip_tax_service import split_settlement_amount_due


def test_split_settlement_amount_due_includes_tip():
    assert split_settlement_amount_due(100_000, 10_000) == 110_000


def test_split_settlement_amount_due_zero_tip():
    assert split_settlement_amount_due(50_000, 0) == 50_000


def test_split_settlement_amount_due_with_tip_tax():
    assert split_settlement_amount_due(100_000, 10_000, 1_900) == 111_900
