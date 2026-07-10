"""Unit tests for FE presentation helper — emisor = tenant fiscal only."""
from datetime import date, datetime, timezone
from uuid import UUID

from app.services.invoicing_presentation import (
    build_invoice_presentation,
    commercial_header_name,
    format_issuer_label,
    resolve_tenant_issuer,
)
from app.services.invoicing_presentation.casting import as_optional_int, clean_str, date_iso


def test_resolve_issuer_uses_fiscal_only_not_display_name():
    fiscal = {
        "business_name": "Rebel Rebel SAS",
        "nit": "1019023777-3",
        "fiscal_address": "Cra 40",
        "city": "Bogotá",
        "email": "fe@rebel.com",
        "matias_company_id": "2bd6053a-6e4a-4c00-8000-000000000001",
    }
    issuer = resolve_tenant_issuer(fiscal)
    assert issuer["name"] == "Rebel Rebel SAS"
    assert issuer["fiscal_id"] == "1019023777-3"
    assert issuer["fiscal_id_type"] == "NIT"
    assert issuer["email"] == "fe@rebel.com"


def test_resolve_issuer_ignores_display_name_when_no_fiscal():
    """display_name must not become emisor; empty fiscal → empty issuer."""
    row = {"display_name": "Waro Colombia", "business_name": None, "nit": None}
    issuer = resolve_tenant_issuer(row)
    assert issuer["name"] is None
    assert issuer["fiscal_id"] is None
    assert format_issuer_label(issuer) is None


def test_resolve_issuer_accepts_fiscal_business_name_alias():
    row = {"fiscal_business_name": "Tenant FE SAS", "nit": "900111222"}
    issuer = resolve_tenant_issuer(row)
    assert issuer["name"] == "Tenant FE SAS"
    assert issuer["fiscal_id"] == "900111222"


def test_clean_str_and_int_casting():
    assert clean_str(UUID("8a54e54e-6f5a-4c00-8000-000000000099")) == (
        "8a54e54e-6f5a-4c00-8000-000000000099"
    )
    assert clean_str("  ") is None
    assert as_optional_int("5462") == 5462
    assert as_optional_int(None) is None
    assert date_iso(date(2026, 1, 15)) == "2026-01-15"
    assert date_iso(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)) == "2026-01-15"


def test_matias_builder_sets_provider_meta_and_fiscal_issuer():
    invoice = {
        "prefix": "LZT",
        "invoice_number": 100,
        "cufe": "ABC",
        "status": "accepted",
        "r2_pdf_key": "k.pdf",
        "r2_xml_key": None,
        "emitted_at": datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
    }
    order = {
        "customer_name": "Cliente",
        "customer_fiscal_id": None,
        "payment_method": "cash",
        "payment_status": "paid",
    }
    fiscal = {
        "business_name": "Mi Restaurante SAS",
        "nit": "900999888",
        "matias_company_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    public = {"display_name": "Mi Local Marketing"}

    presentation = build_invoice_presentation(
        invoice_row=invoice,
        order_row=order,
        fiscal_row=fiscal,
        public_profile=public,
        resolution_row={
            "resolution_number": "1876",
            "prefix": "LZT",
            "from_number": 1,
            "to_number": 5000,
            "date_from": date(2026, 1, 1),
            "date_to": date(2026, 12, 31),
        },
        tax_details=[{"label": "IVA 19%", "base": 100.0, "amount": 19.0}],
        provider="matias",
        serialize_datetimes=True,
    )

    assert presentation["issuer"]["name"] == "Mi Restaurante SAS"
    assert presentation["issuer"]["fiscal_id"] == "900999888"
    # Must NOT use marketing display_name as issuer
    assert presentation["issuer"]["name"] != "Mi Local Marketing"
    assert presentation["provider_meta"]["facturador"] == "matias"
    assert presentation["provider_meta"]["technology_platform"] == "waro"
    assert presentation["provider_meta"]["matias_client_uuid"] == (
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    assert presentation["number"] == "LZT-100"
    assert presentation["attachments"] == {"pdf": True, "xml": False}
    assert presentation["resolution"]["number"] == "1876"
    assert presentation["resolution"]["date_from"] == "2026-01-01"
    assert isinstance(presentation["emitted_at"], str)


def test_matias_builder_keeps_datetime_when_not_serialized():
    invoice = {
        "prefix": "A",
        "invoice_number": 1,
        "cufe": "X",
        "status": "accepted",
        "emitted_at": datetime(2026, 5, 13, 20, 44, tzinfo=timezone.utc),
    }
    presentation = build_invoice_presentation(
        invoice_row=invoice,
        order_row={},
        fiscal_row={"business_name": "FE SAS", "nit": "1"},
        serialize_datetimes=False,
    )
    assert isinstance(presentation["emitted_at"], datetime)


def test_commercial_header_prefer_fiscal_for_fe_email():
    fiscal = {"business_name": "FE Legal SAS"}
    public = {"display_name": "Marca Local"}
    assert commercial_header_name(
        fiscal_row=fiscal, public_profile=public, prefer_fiscal=True
    ) == "FE Legal SAS"
    assert commercial_header_name(
        fiscal_row=fiscal, public_profile=public, prefer_fiscal=False
    ) == "Marca Local"


def test_format_issuer_label():
    assert format_issuer_label({"name": "A", "fiscal_id": "1"}) == "A - NIT 1"
    assert format_issuer_label({"name": None, "fiscal_id": None}) is None
