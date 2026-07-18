"""Unit tests for cierre preview cashExpected (warocol.com#948)."""
from app.services.cierre_service import (
    _advance_audit_totals,
    _apply_table_session_advances_to_methods,
    _compute_cash_expected,
)


def test_cash_expected_all_cash_tips_embedded_in_total_cash():
    """Repro: opening 200k + totalCash 459k (sales+tips) must not add cashTips again."""
    result = _compute_cash_expected(
        opening_cash=200_000.0,
        total_cash=459_000.0,
        gastos_efectivo=0.0,
    )
    assert result == 659_000.0


def test_cash_expected_marimba_mixed_payments_does_not_duplicate_cash_tips():
    """Marimba: totalCash 640.9k already contains the 41.9k cash tip settlement."""
    result = _compute_cash_expected(
        opening_cash=10_000.0,
        total_cash=640_900.0,
        gastos_efectivo=0.0,
    )
    assert result == 650_900.0


def test_cash_expected_mixed_pay_cash_portion_without_cash_tips():
    result = _compute_cash_expected(
        opening_cash=50_000.0,
        total_cash=100_000.0,
        gastos_efectivo=10_000.0,
    )
    assert result == 140_000.0


def test_cash_expected_subtracts_expenses_and_cash_purchases_once():
    result = _compute_cash_expected(
        opening_cash=100_000.0,
        total_cash=200_000.0,
        gastos_efectivo=10_000.0,
        cash_purchases=20_000.0,
    )
    assert result == 270_000.0


def test_table_advance_partial_close_moves_amount_to_advance_tender():
    adjusted = _apply_table_session_advances_to_methods(
        {"cash": 100_000.0},
        {
            "applications": {"cash": 60_000.0},
            "collections": {"digital": 60_000.0},
            "cover": {"total": 0.0},
        },
    )

    assert adjusted["cash"] == 40_000.0
    assert adjusted["digital"] == 60_000.0


def test_table_advance_exact_close_zeroes_synthetic_settlement_method():
    adjusted = _apply_table_session_advances_to_methods(
        {"table_session_advance": 60_000.0},
        {
            "applications": {"table_session_advance": 60_000.0},
            "collections": {"cash": 60_000.0},
            "cover": {"total": 0.0},
        },
    )

    assert adjusted["table_session_advance"] == 0.0
    assert adjusted["cash"] == 60_000.0


def test_table_advance_overage_keeps_collection_and_reports_cover():
    advance_totals = {
        "applications": {"table_session_advance": 28_000.0},
        "collections": {"digital": 60_000.0},
        "cover": {"total": 32_000.0},
    }
    adjusted = _apply_table_session_advances_to_methods(
        {"table_session_advance": 28_000.0},
        advance_totals,
    )
    audit = _advance_audit_totals(advance_totals)

    assert adjusted["table_session_advance"] == 0.0
    assert adjusted["digital"] == 60_000.0
    assert audit == {
        "tableAdvanceCollections": 60_000.0,
        "tableAdvanceApplications": 28_000.0,
        "tableAdvanceCover": 32_000.0,
    }
