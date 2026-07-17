from datetime import datetime, timezone

import pytest

from app.services.email_helpers import send_pos_receipt_email


class FakeConn:
    async def fetchrow(self, query, *_args):
        if "tenant_public_profiles" in query:
            return {"locale": "en", "currency_code": "COP", "timezone": "America/Bogota"}
        if "tenant_fiscal_data" in query:
            return None
        if "electronic_invoices" in query:
            return {
                "id": "invoice-1",
                "r2_pdf_key": "invoices/test.pdf",
                "r2_xml_key": "invoices/test.xml",
                "prefix": "TEST",
                "invoice_number": 123,
            }
        return None


class FakeDbContext:
    async def __aenter__(self):
        return FakeConn()

    async def __aexit__(self, *_exc):
        return False


class FakeSES:
    sent = {}

    async def send_email(self, **kwargs):
        FakeSES.sent = kwargs
        return True

    async def send_email_with_attachments(self, **kwargs):
        FakeSES.sent = kwargs
        return True


class FakeStorage:
    async def get_object_bytes(self, key):
        if key.endswith(".pdf"):
            return b"%PDF-test"
        if key.endswith(".xml"):
            return b"<xml/>"
        return None


async def fake_sender_email(_tenant_id):
    return "no-reply@example.com"


@pytest.mark.asyncio
async def test_send_pos_receipt_email_resolves_locale_from_tenant(monkeypatch):
    monkeypatch.setattr("app.services.email_helpers.get_db_connection", lambda **_kwargs: FakeDbContext())
    monkeypatch.setattr("app.services.email_helpers.AWSSESService", FakeSES)
    monkeypatch.setattr("app.services.email_helpers.resolve_sender_email_for_tenant", fake_sender_email)

    success = await send_pos_receipt_email(
        customer_email="customer@example.com",
        order_number=16387,
        total_amount=311000,
        payment_method="card",
        items=[{"quantity": 1, "subtotal": 311000, "product": {"name": "Santa inquisición"}}],
        order_date=datetime(2026, 7, 12, 1, 51, tzinfo=timezone.utc),
        tenant_id="tenant-1",
        business_name="Waro Colombia",
    )

    assert success is True
    assert FakeSES.sent["subject"] == "Purchase receipt #16387 — Waro Colombia"
    assert "Date: July 11, 2026, 8:51 PM" in FakeSES.sent["text_body"]
    assert "Payment method: Card" in FakeSES.sent["text_body"]
    assert "Método de pago" not in FakeSES.sent["text_body"]


@pytest.mark.asyncio
async def test_send_pos_receipt_email_always_attaches_available_pdf(monkeypatch):
    monkeypatch.setenv("INVOICE_PDF_ENABLED", "false")
    monkeypatch.setattr("app.services.email_helpers.get_db_connection", lambda **_kwargs: FakeDbContext())
    monkeypatch.setattr("app.services.email_helpers.AWSSESService", FakeSES)
    monkeypatch.setattr("app.services.email_helpers.AWSS3Service", FakeStorage)
    monkeypatch.setattr("app.services.email_helpers.resolve_sender_email_for_tenant", fake_sender_email)

    result = await send_pos_receipt_email(
        customer_email="customer@example.com",
        order_number=123,
        total_amount=100000,
        payment_method="card",
        items=[{"quantity": 1, "subtotal": 100000, "product": {"name": "Producto"}}],
        order_date=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        tenant_id="tenant-1",
        business_name="Waro Colombia",
        invoice_prefix="TEST",
        invoice_number=123,
        invoice_cufe="CUFE-123",
        return_details=True,
    )

    assert result == {
        "success": True,
        "attachments": {"pdf": True, "xml": True},
        "attachment_warnings": [],
    }
    assert FakeSES.sent["attachments"] == [
        {
            "data": b"%PDF-test",
            "filename": "TEST-123.pdf",
            "content_type": "application/pdf",
        },
        {
            "data": b"<xml/>",
            "filename": "TEST-123.xml",
            "content_type": "application/xml",
        },
    ]
