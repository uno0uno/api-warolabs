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


def test_pos_receipt_renders_waro_redemption_line():
    text = get_pos_receipt_text(
        order_number=44,
        total_amount=45000,
        payment_method="cash",
        items=[{"quantity": 1, "subtotal": 50000, "product": {"name": "Combo"}}],
        order_date=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        subtotal=50000,
        waro_redemption_summary={
            "waro_discount_cop": 5000.0,
            "waros_spent": 200,
            "waro_breakdown": [
                {
                    "redemption_type": "reward_fixed_cop",
                    "waros_spent": 200,
                    "cop_discount": 5000.0,
                    "reward_name": "Empanada gratis",
                },
            ],
        },
    )
    assert "Subtotal: $50.000" in text
    assert "Canje WaRo (Empanada gratis): -$5.000" in text


def test_pos_receipt_labels_manual_discount_separately_from_promotions():
    text = get_pos_receipt_text(
        order_number=45,
        total_amount=82000,
        payment_method="card",
        items=[{"quantity": 1, "subtotal": 100000, "product": {"name": "Combo"}}],
        order_date=datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc),
        subtotal=100000,
        promo_breakdown=[
            {
                "promotion_name": "Promo almuerzo",
                "promo_type": "percentage",
                "savings": 8000,
            },
        ],
        discount_amount=10000,
    )
    assert "Promo almuerzo: -$8.000" in text
    assert "Descuento manual: -$10.000" in text
    assert "Descuento: -$10.000" not in text


def test_pos_receipt_keeps_checkout_discount_stack_separate():
    text = get_pos_receipt_text(
        order_number=46,
        total_amount=83000,
        payment_method="customer_wallet",
        items=[{"quantity": 1, "subtotal": 120000, "product": {"name": "Cena"}}],
        order_date=datetime(2026, 6, 19, 18, 30, tzinfo=timezone.utc),
        subtotal=120000,
        promo_breakdown=[
            {
                "promotion_name": "Promo noche",
                "promo_type": "fixed_off",
                "savings": 12000,
            },
        ],
        discount_amount=15000,
        standard_tax=3000,
        standard_tax_label="IVA",
        tip_amount=7000,
        waro_redemption_summary={
            "waro_discount_cop": 10000,
            "waro_breakdown": [
                {
                    "redemption_type": "reward_fixed_cop",
                    "cop_discount": 10000,
                    "reward_name": "Bono fidelidad",
                },
            ],
        },
    )

    assert "Subtotal: $120.000" in text
    assert "Promo noche: -$12.000" in text
    assert "Descuento manual: -$15.000" in text
    assert "Canje WaRo (Bono fidelidad): -$10.000" in text
    assert "IVA: $3.000" in text
    assert "Propina: $7.000" in text
    assert "TOTAL COBRADO: $90.000" in text
