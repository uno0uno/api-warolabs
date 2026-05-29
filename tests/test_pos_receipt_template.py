"""Tests for POS receipt template tip label (warocol.com#977)."""
from datetime import datetime, timezone

from app.templates.pos_receipt_template import get_pos_receipt_text


def test_pos_receipt_uses_custom_tip_label():
    text = get_pos_receipt_text(
        order_number=42,
        total_amount=100000,
        payment_method="cash",
        items=[{"quantity": 1, "subtotal": 100000, "product": {"name": "Cafe"}}],
        order_date=datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
        tip_amount=10000,
        tip_label="Servicio",
    )
    assert "Servicio: $10.000" in text
    assert "Propina:" not in text


def test_pos_receipt_tip_label_defaults_to_propina():
    text = get_pos_receipt_text(
        order_number=43,
        total_amount=50000,
        payment_method="card",
        items=[{"quantity": 1, "subtotal": 50000, "product": {"name": "Agua"}}],
        order_date=datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
        tip_amount=5000,
    )
    assert "Propina: $5.000" in text
