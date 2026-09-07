from app.services.table_session_guests import (
    guest_snapshot_from_capacity,
    normalize_custom_label,
)


def test_guest_snapshot_uses_catalog_capacity():
    covers, snap = guest_snapshot_from_capacity(4)
    assert covers == 4
    assert snap == 4


def test_guest_snapshot_defaults_when_capacity_missing():
    covers, snap = guest_snapshot_from_capacity(None)
    assert covers == 1
    assert snap is None


def test_guest_snapshot_ignores_non_positive():
    covers, snap = guest_snapshot_from_capacity(0)
    assert covers == 1
    assert snap is None


def test_normalize_custom_label_blank_is_none():
    assert normalize_custom_label("  ") is None
    assert normalize_custom_label("Hab 12") == "Hab 12"
