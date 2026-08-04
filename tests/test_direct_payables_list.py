"""Unpaid direct payables list scoping for Pagos (#2110 / epic #2109)."""

from app.services.purchase_tracking_service import validate_state_transition
from app.services.purchases_service import (
    _DIRECT_PAYABLES_SQL,
    _EXCLUDE_DIRECTS_SQL,
    direct_entry_list_clause,
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
    # Still keeps classic non-direct rows
    assert "is_direct_entry = FALSE OR tp.is_direct_entry IS NULL" in clause.replace("\n", " ")


def test_received_to_paid_transition_allowed_for_direct_credito_flow():
    """Pay path for directs uses the same state machine as supplier credit."""
    assert validate_state_transition("received", "paid") is True
    assert validate_state_transition("paid", "received") is False
