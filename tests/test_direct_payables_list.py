"""Unpaid direct payables list scoping for Pagos (#2110 / epic #2109)."""

from app.services.purchase_tracking_service import validate_state_transition
from app.services.purchases_service import (
    _DIRECT_PAYABLES_SQL,
    _EXCLUDE_DIRECTS_SQL,
    direct_entry_list_clause,
    row_matches_purchases_list_scope,
)


def test_default_list_excludes_directs():
    clause = direct_entry_list_clause(False)
    assert clause == _EXCLUDE_DIRECTS_SQL
    assert "is_direct_entry = FALSE" in clause
    assert "paid_at IS NULL" not in clause


def test_include_direct_payables_allows_unpaid_received_non_contado():
    clause = direct_entry_list_clause(True)
    assert clause == _DIRECT_PAYABLES_SQL
    assert "paid_at IS NULL" in clause
    assert "status = 'received'" in clause
    assert "IS DISTINCT FROM 'contado'" in clause


def test_scope_matrix_default_excludes_all_directs():
    assert row_matches_purchases_list_scope(
        is_direct_entry=False, paid_at=None, status="received",
        payment_type="credito", include_direct_payables=False,
    )
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at=None, status="received",
        payment_type="credito", include_direct_payables=False,
    )


def test_scope_matrix_flag_includes_unpaid_direct_credito_only():
    # unpaid direct crédito received → included
    assert row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at=None, status="received",
        payment_type="credito", include_direct_payables=True,
    )
    # paid contado direct → excluded
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at="2026-08-01", status="paid",
        payment_type="contado", include_direct_payables=True,
    )
    # unpaid contado received (shouldn't happen often) → excluded
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at=None, status="received",
        payment_type="contado", include_direct_payables=True,
    )
    # classic non-direct still included
    assert row_matches_purchases_list_scope(
        is_direct_entry=False, paid_at=None, status="confirmed",
        payment_type="contado", include_direct_payables=True,
    )
    # already-paid direct crédito → excluded from list scope
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at="2026-08-01", status="paid",
        payment_type="credito", include_direct_payables=True,
    )


def test_received_to_paid_transition_allowed_for_direct_credito_flow():
    """Pay path for directs uses the same state machine as supplier credit."""
    assert validate_state_transition("received", "paid") is True
    assert validate_state_transition("paid", "received") is False
