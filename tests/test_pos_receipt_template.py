"""Tests for POS receipt template tip label (warocol.com#977) and promo totals (#337)."""
from datetime import datetime, timezone

from app.services.orders_service import _compute_tax_breakdown
from app.templates.pos_receipt_template import get_pos_receipt_text


def _sample_item(name: str = "Cafe", subtotal: float = 100000):
    return {"quantity": 1, "subtotal": subtotal, "product": {"name": name}}


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


def test_pos_receipt_promo_only_shows_promo_lines_without_manual_discount():
    """Promo-only order renders gross subtotal + promo savings, no Descuento line."""
    text = get_pos_receipt_text(
        order_number=44,
        total_amount=90000,
        payment_method="cash",
        items=[_sample_item(subtotal=100000)],
        order_date=datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
        subtotal=100000,
        promo_savings=10000,
        promo_breakdown=[
            {
                "promotion_name": "2x1 cervezas",
                "promo_type": "bogo",
                "savings": 10000,
            }
        ],
        standard_tax=14370,
        standard_tax_label="IVA 19%",
    )
    assert "Subtotal: $100.000" in text
    assert "2x1 cervezas: -$10.000" in text
    assert "Descuento:" not in text
    assert "IVA 19%: $14.370" in text
    assert "TOTAL: $90.000" in text


def test_compute_tax_breakdown_prefers_net_line_base():
    """Tax on net_total must be lower than gross subtotal for the same order."""
    tax_config = {
        "inc_applicable": False,
        "iva_applicable": True,
        "iva_rate": 0.19,
        "iva_included_in_price": True,
        "liquor_tax_applicable": False,
    }
    gross_rows = [{"tax_category": "standard", "subtotal": 100000.0}]
    net_rows = [{"tax_category": "standard", "subtotal": 90000.0}]

    gross_tax, _, _ = _compute_tax_breakdown(gross_rows, tax_config)
    net_tax, _, label = _compute_tax_breakdown(net_rows, tax_config)

    assert label == "IVA 19%"
    assert net_tax < gross_tax
    assert net_tax == 14370
    assert gross_tax == 15966
