"""Unit tests for cierre preview cashExpected (warocol.com#948)."""
from app.services.cierre_service import _compute_cash_expected


def test_cash_expected_all_cash_tips_embedded_in_total_cash():
    """Repro: opening 200k + totalCash 459k (sales+tips) must not add cashTips again."""
    result = _compute_cash_expected(
        opening_cash=200_000.0,
        total_cash=459_000.0,
        cash_tips=99_000.0,
        total_sales=360_000.0,
        gastos_efectivo=0.0,
    )
    assert result == 659_000.0


def test_cash_expected_split_pay_cash_sales_only():
    """Regression: cash portion excludes tips settled on card — keep additive branch."""
    result = _compute_cash_expected(
        opening_cash=50_000.0,
        total_cash=100_000.0,
        cash_tips=0.0,
        total_sales=360_000.0,
        gastos_efectivo=10_000.0,
    )
    assert result == 140_000.0


def test_cash_expected_exact_equality_uses_embedded_branch():
    result = _compute_cash_expected(
        opening_cash=0.0,
        total_cash=459_000.0,
        cash_tips=99_000.0,
        total_sales=360_000.0,
        gastos_efectivo=5_000.0,
    )
    assert result == 454_000.0


def test_cash_expected_additive_when_cash_tips_not_embedded():
    """When total_cash is sales-only, cash_tips must be added."""
    result = _compute_cash_expected(
        opening_cash=100_000.0,
        total_cash=200_000.0,
        cash_tips=30_000.0,
        total_sales=360_000.0,
        gastos_efectivo=0.0,
    )
    assert result == 330_000.0
