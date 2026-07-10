"""Tests for POS receipt template tip label (warocol.com#977)."""
from datetime import datetime, timezone

import pytest

from app.templates.pos_receipt_template import get_pos_receipt_text


@pytest.fixture
def platform_print_env(monkeypatch):
    monkeypatch.setenv("WARO_LEGAL_COMMERCIAL_NAME", "WARO COLOMBIA")
    monkeypatch.setenv("WARO_LEGAL_LEGAL_NAME", "AREVALO TEST")
    monkeypatch.setenv("WARO_LEGAL_NIT", "700128766-3")
    monkeypatch.setenv("WARO_LEGAL_IVA_LABEL", "No responsable de IVA")
    monkeypatch.setenv("FACTURADOR_LEGAL_BRAND_NAME", "Matias API")
    monkeypatch.setenv("FACTURADOR_LEGAL_LEGAL_NAME", "LOPEZSOFT S.A.S.")
    monkeypatch.setenv("FACTURADOR_LEGAL_NIT", "901.091.403-2")
    from app import config as config_mod
    config_mod.settings = config_mod.Settings()
    yield
    config_mod.settings = config_mod.Settings()


def test_pos_receipt_uses_custom_tip_label(platform_print_env):
    text = get_pos_receipt_text(
        order_number=42,
        total_amount=100000,
        payment_method="cash",
        items=[{"quantity": 1, "subtotal": 100000, "product": {"name": "Cafe"}}],
        order_date=datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
        tip_amount=10000,
        tip_label="Servicio",
        business_name="Mi Restaurante SAS",
    )
    assert "Servicio: $10.000" in text
    assert "Propina:" not in text
    # Sin FE: comprobante + pie WARO (tecnología, no emisor)
    assert "COMPROBANTE DE VENTA" in text
    assert "No es factura electrónica DIAN" in text
    assert "WARO COLOMBIA" in text
    assert "700128766-3" in text
    assert "No es el emisor de esta venta" in text
    # Sin FE no se exige bloquear Matias en el pie comercial
    assert "LOPEZSOFT" not in text


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


def test_pos_receipt_renders_fiscal_invoice_presentation(platform_print_env):
    text = get_pos_receipt_text(
        order_number=47,
        total_amount=120000,
        payment_method="cash",
        items=[{"quantity": 1, "subtotal": 120000, "product": {"name": "Cena"}}],
        order_date=datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc),
        invoice_prefix="LZT",
        invoice_number=5462,
        invoice_cufe="CUFE123",
        invoice_presentation={
            "status": "accepted",
            "emitted_at": datetime(2026, 7, 1, 18, 5, tzinfo=timezone.utc),
            "dian_url": "https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey=CUFE123",
            "issuer": {
                "name": "Waro Colombia SAS",
                "fiscal_id_type": "NIT",
                "fiscal_id": "901234567",
                "address": "Carrera 10 #20-30",
                "city": "Bogotá",
                "email": "facturacion@warocol.com",
            },
            "acquirer": {
                "name": "Restaurante Cliente SAS",
                "fiscal_id_type": "NIT",
                "fiscal_id": "900123456",
                "email": "contabilidad@example.com",
            },
            "resolution": {
                "number": "18760000001",
                "prefix": "LZT",
                "from_number": 1,
                "to_number": 9999,
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
            },
            "tax_details": [
                {"label": "IVA 19%", "base": 100000, "amount": 19000},
            ],
            "attachments": {"pdf": True, "xml": True},
        },
    )

    assert "FACTURA ELECTRÓNICA DE VENTA" in text
    assert "Representación gráfica para verificación contable." in text
    assert "Número: LZT-5462" in text
    assert "Estado DIAN: accepted" in text
    assert "Emisor:" in text
    assert "Waro Colombia SAS" in text
    assert "Adquirente:" in text
    assert "Restaurante Cliente SAS" in text
    assert "Resolución DIAN: 18760000001" in text
    assert "IVA 19% base $100.000: $19.000" in text
    assert "Archivos: PDF adjunto, XML adjunto" in text
    assert "Verificar en DIAN: https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey=CUFE123" in text
    # Pie software WARO + note Matias (tecnología ≠ emisor)
    assert "700128766-3" in text
    assert "No es el emisor de esta venta" in text
    assert "Matias API" in text
