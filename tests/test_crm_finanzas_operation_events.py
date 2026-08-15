"""Bitácora CUD writers for CRM / finanzas / facturación (warocol.com#2326)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.models.customer import CustomerUpdate
from app.services import (
    accounting_service,
    credit_service,
    customers_service,
    expenses_service,
    facturacion_service,
)


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, extra, tb):
        return False


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _capture():
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append({"tenant_id": tid, **kwargs})

    return recorded, capture_record


@pytest.mark.asyncio
async def test_search_or_create_existing_customer_does_not_record():
    tenant_id = uuid4()
    customer_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": customer_id,
        "phone_number": "3001234567",
        "name": "Ana",
        "email": "ana@example.com",
        "fiscal_id_type": None,
        "fiscal_id": None,
        "fiscal_business_name": None,
        "fiscal_email": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })

    body = SimpleNamespace(
        phone_number="3001234567",
        name="Ana",
        email=None,
        fiscal_id_type=None,
        fiscal_id=None,
        fiscal_business_name=None,
        fiscal_email=None,
    )

    with patch("app.services.customers_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.customers_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.customers_service.upsert_tenant_customer", new=AsyncMock()), \
         patch("app.services.customers_service.record_operation_event", new=capture_record):
        result = await customers_service.search_or_create_customer(Request({"type": "http"}), body)

    assert result.is_new is False
    assert recorded == []


@pytest.mark.asyncio
async def test_update_customer_records_crm_event():
    tenant_id = uuid4()
    user_id = uuid4()
    customer_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": customer_id},
        {
            "id": customer_id,
            "phone_number": "3001234567",
            "name": "Ana Lopez",
            "email": "ana@example.com",
            "fiscal_id_type": None,
            "fiscal_id": None,
            "fiscal_business_name": None,
            "fiscal_email": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
    ])

    with patch("app.services.customers_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.customers_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.customers_service.record_operation_event", new=capture_record):
        result = await customers_service.update_customer(
            Request({"type": "http"}),
            customer_id,
            CustomerUpdate(name="Ana Lopez"),
        )

    assert result.success is True
    assert len(recorded) == 1
    assert recorded[0]["domain"] == "crm"
    assert recorded[0]["action"] == "customer_updated"
    assert recorded[0]["payload"]["entity_id"] == str(customer_id)


@pytest.mark.asyncio
async def test_delete_expense_records_finanzas_event():
    tenant_id = uuid4()
    expense_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(return_value={"transaction_date": date(2026, 8, 1)})
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="DELETE 1")

    with patch("app.services.expenses_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.expenses_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.expenses_service._void_expense_gl_entry", new=AsyncMock()), \
         patch("app.services.expenses_service.record_operation_event", new=capture_record):
        result = await expenses_service.delete_expense(
            Request({"type": "http"}),
            MagicMock(),
            expense_id,
        )

    assert result["success"] is True
    assert len(recorded) == 1
    assert recorded[0]["action"] == "expense_deleted"
    assert recorded[0]["domain"] == "finanzas"


@pytest.mark.asyncio
async def test_get_credit_payments_does_not_record():
    tenant_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "total_amount": 50000,
        "payment_status": "credit",
        "credit_paid_amount": 0,
    })
    conn.fetch = AsyncMock(return_value=[])

    with patch("app.services.credit_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.credit_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.credit_service.record_operation_event", new=capture_record):
        result = await credit_service.get_credit_payments(Request({"type": "http"}), order_id)

    assert result["success"] is True
    assert recorded == []


@pytest.mark.asyncio
async def test_get_journal_entry_does_not_record():
    tenant_id = uuid4()
    entry_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": entry_id,
        "tenant_id": tenant_id,
        "entry_date": date(2026, 8, 1),
        "period_year": 2026,
        "period_month": 8,
        "description": "Ajuste",
        "reference": None,
        "source_module": "manual",
        "source_id": None,
        "status": "draft",
        "posted_at": None,
        "posted_by": None,
        "voided_at": None,
        "voided_by": None,
        "void_reason": None,
        "total_debit": 0,
        "total_credit": 0,
        "created_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    conn.fetch = AsyncMock(return_value=[])

    with patch("app.services.accounting_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.accounting_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.accounting_service.record_operation_event", new=capture_record):
        result = await accounting_service.get_journal_entry(Request({"type": "http"}), entry_id)

    assert result.data is not None
    assert recorded == []


@pytest.mark.asyncio
async def test_emit_invoice_already_accepted_does_not_record():
    order_id = str(uuid4())
    tenant_id = str(uuid4())
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": order_id,
            "status": "completed",
            "payment_method": "cash",
            "payment_status": "paid",
            "active_payment_count": 1,
        },
        {
            "status": "accepted",
            "invoice_number": "1",
            "prefix": "SETT",
            "error_message": None,
            "created_at": datetime.now(timezone.utc),
        },
    ])

    with patch("app.services.facturacion_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.facturacion_service.get_order_invoice", new=AsyncMock(return_value={"status": "accepted"})), \
         patch("app.services.facturacion_service.record_operation_event", new=capture_record):
        result = await facturacion_service.emit_invoice(order_id, tenant_id)

    assert result["status"] == "accepted"
    assert recorded == []


@pytest.mark.asyncio
async def test_emit_invoice_success_records_facturacion_event():
    order_id = str(uuid4())
    tenant_id = str(uuid4())
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": order_id,
            "status": "completed",
            "payment_method": "cash",
            "payment_status": "paid",
            "active_payment_count": 1,
        },
        None,
    ])

    class _Resp:
        status_code = 200
        is_success = True
        text = "{}"

        def json(self):
            return {"status": "accepted", "cufe": "abc", "invoice_number": "100"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            return _Resp()

    with patch("app.services.facturacion_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.facturacion_service.httpx.AsyncClient", return_value=_Client()), \
         patch("app.services.facturacion_service.record_operation_event", new=capture_record):
        result = await facturacion_service.emit_invoice(order_id, tenant_id)

    assert result["status"] == "accepted"
    assert len(recorded) == 1
    assert recorded[0]["domain"] == "facturacion"
    assert recorded[0]["action"] == "invoice_emitted"
