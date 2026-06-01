"""Data quality anomaly detection (#46)."""
import pytest

from app.services.analytics_service import _check_unit_mismatch, _compute_anomaly


def test_compute_anomaly_impossible_value():
    result = _compute_anomaly([10.0, 12.0, 11.0, 10.5, 11.5], -1.0, "Test")
    assert result is not None
    assert result["alert_type"] == "impossible_value"
    assert result["severity"] == "critical"


def test_compute_anomaly_insufficient_history():
    assert _compute_anomaly([10.0], 100.0, "Test") is None


def test_compute_anomaly_warning_deviation():
    history = [10.0, 10.0, 10.0, 10.0, 10.0]
    result = _compute_anomaly(history, 13.0, "Test")
    assert result is not None
    assert result["severity"] == "warning"
    assert result["alert_type"] == "price_spike"
    assert result["deviation_pct"] == 30.0


def test_compute_anomaly_critical_deviation():
    history = [10.0, 10.0, 10.0, 10.0, 10.0]
    result = _compute_anomaly(history, 16.0, "Test")
    assert result is not None
    assert result["severity"] == "critical"


def test_compute_anomaly_no_flag_within_threshold():
    history = [10.0, 10.0, 10.0, 10.0, 10.0]
    assert _compute_anomaly(history, 11.0, "Test") is None


def test_check_unit_mismatch_flags_implausible_unit_cost():
    result = _check_unit_mismatch(
        purchase_quantity=1.0,
        base_quantity=1.0,
        purchase_unit="ml",
        ingredient_base_unit="ml",
        unit_cost=7850.0,
    )
    assert result is not None
    assert result["alert_type"] == "unit_mismatch"
    assert result["severity"] == "critical"


def test_check_unit_mismatch_skips_when_conversion_applied():
    assert _check_unit_mismatch(
        purchase_quantity=1.0,
        base_quantity=3000.0,
        purchase_unit="l",
        ingredient_base_unit="ml",
        unit_cost=7850.0,
    ) is None
