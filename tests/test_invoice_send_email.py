"""
Tests for POST /api/orders/{order_id}/invoice/send-email (warocol.com#603).

The endpoint orchestrates: validate session → load order + invoice + items +
tax + profile from DB → call send_pos_receipt_email (existing SES helper).

We patch:
  - app.services.orders_service.require_valid_session → fake session
  - app.services.orders_service.get_db_connection → controlled fetch responses
  - app.services.orders_service._get_tenant_tax_config → minimal config
  - app.services.orders_service.send_pos_receipt_email → AsyncMock (True/False)
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.services import orders_service


_TENANT_ID = UUID('93b3e582-34fa-44a6-8d0f-bf82a3608727')
_ORDER_ID = uuid4()
_RECIPIENT = 'anderson.electronico@gmail.com'


class _SeqConnCtx:
    """Async context manager that hands a conn whose fetchrow/fetch are
    pre-loaded with sequenced responses."""

    def __init__(self, fetchrow_responses, fetch_responses):
        self._frows = iter(fetchrow_responses)
        self._fetch = iter(fetch_responses)

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=lambda *a, **k: next(self._frows))
        conn.fetch = AsyncMock(side_effect=lambda *a, **k: next(self._fetch))
        return conn

    async def __aexit__(self, *_):
        return False


def _order_row(discount: float = 0.0):
    return {
        'id': _ORDER_ID,
        'order_number': 5462,
        'order_date': datetime(2026, 5, 13, 20, 43, tzinfo=timezone.utc),
        'total_amount': 200.0,
        'payment_method': 'cash',
        'discount_amount': discount,
    }


def _invoice_row(status: str = 'accepted', r2_pdf_key: str = 'lzt/5462/test.pdf'):
    return {
        'prefix': 'LZT',
        'invoice_number': 5462,
        'cufe': 'TEST_CUFE',
        'status': status,
        'r2_pdf_key': r2_pdf_key,
    }


def _profile_row():
    return {
        'display_name': 'Waro Colombia',
        'address': 'Calle 123 #45-67',
        'city': 'Bogotá',
        'phone_number': '+57 320 1234567',
    }


def _waro_inferred_row(discount: float = 0.0):
    return {
        'waro_discount_cop': discount,
    }


def _patch_session():
    fake = MagicMock()
    fake.tenant_id = _TENANT_ID
    return patch(
        'app.services.orders_service.require_valid_session',
        return_value=fake,
    )


def _patch_db(*, fetchrow, fetch):
    ctx = _SeqConnCtx(fetchrow, fetch)
    return patch(
        'app.services.orders_service.get_db_connection',
        lambda *args, **kwargs: ctx,
    )


def _patch_tax():
    return patch(
        'app.services.orders_service._get_tenant_tax_config',
        new=AsyncMock(return_value={
            'inc_applicable': False, 'iva_applicable': False,
            'liquor_tax_applicable': False,
            'inc_included_in_price': True, 'iva_included_in_price': True,
        }),
    )


# ── Happy path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_invoice_email_happy_path():
    """Owned order + accepted invoice + PDF → 200, helper called with full payload."""
    request = MagicMock()

    fetchrow = [_order_row(), _invoice_row(), _waro_inferred_row(), _profile_row()]
    fetch = [
        [],  # tax items (empty → no tax)
        [],  # promo summary (no applied promos)
        [],  # waro redemption summary (none)
        [{'id': uuid4(), 'quantity': 1, 'subtotal': 200.0, 'product_name': 'tomate barranca'}],
        [],  # modifiers for item
    ]
    helper_mock = AsyncMock(return_value=True)

    with _patch_session(), _patch_db(fetchrow=fetchrow, fetch=fetch), _patch_tax(), patch(
        'app.services.orders_service.send_pos_receipt_email',
        new=helper_mock,
    ):
        result = await orders_service.send_invoice_email(request, _ORDER_ID, _RECIPIENT)

    assert result == {'success': True, 'sent_to': _RECIPIENT}
    helper_mock.assert_awaited_once()
    kwargs = helper_mock.await_args.kwargs
    assert kwargs['customer_email'] == _RECIPIENT
    assert kwargs['order_number'] == 5462
    assert kwargs['invoice_prefix'] == 'LZT'
    assert kwargs['invoice_number'] == 5462
    assert kwargs['invoice_cufe'] == 'TEST_CUFE'
    assert kwargs['tenant_id'] == str(_TENANT_ID)
    assert kwargs['business_name'] == 'Waro Colombia'
    # items shape: list of dicts with product.name + quantity + subtotal + modifiers
    assert len(kwargs['items']) == 1
    assert kwargs['items'][0]['product']['name'] == 'tomate barranca'
    assert kwargs['items'][0]['quantity'] == 1
    assert kwargs['items'][0]['modifiers'] == []
    # order_date from the row, NOT datetime.utcnow()
    assert kwargs['order_date'].year == 2026
    assert kwargs['order_date'].month == 5
    assert kwargs['order_date'].day == 13
    assert kwargs['promo_savings'] == 0.0
    assert kwargs['promo_breakdown'] == []


# ── Cross-tenant guard ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_invoice_email_blocks_cross_tenant():
    """SELECT order returns nothing → 404, no helper call."""
    request = MagicMock()
    helper_mock = AsyncMock(return_value=True)

    with _patch_session(), _patch_db(fetchrow=[None], fetch=[]), patch(
        'app.services.orders_service.send_pos_receipt_email',
        new=helper_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await orders_service.send_invoice_email(request, _ORDER_ID, _RECIPIENT)

    assert exc_info.value.status_code == 404
    assert 'Order not found' in exc_info.value.detail
    helper_mock.assert_not_awaited()


# ── No invoice on the order ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_invoice_email_rejects_when_no_invoice():
    """Order exists but has no electronic_invoices row → 422, no helper call."""
    request = MagicMock()
    helper_mock = AsyncMock(return_value=True)

    with _patch_session(), _patch_db(fetchrow=[_order_row(), None], fetch=[]), patch(
        'app.services.orders_service.send_pos_receipt_email',
        new=helper_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await orders_service.send_invoice_email(request, _ORDER_ID, _RECIPIENT)

    assert exc_info.value.status_code == 422
    assert 'no tiene factura' in exc_info.value.detail.lower()
    helper_mock.assert_not_awaited()


# ── Invoice not accepted ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_invoice_email_rejects_non_accepted():
    """Invoice exists but status != 'accepted' → 422."""
    request = MagicMock()
    helper_mock = AsyncMock(return_value=True)

    with _patch_session(), _patch_db(
        fetchrow=[_order_row(), _invoice_row(status='rejected')],
        fetch=[],
    ), patch(
        'app.services.orders_service.send_pos_receipt_email',
        new=helper_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await orders_service.send_invoice_email(request, _ORDER_ID, _RECIPIENT)

    assert exc_info.value.status_code == 422
    assert 'aceptada' in exc_info.value.detail.lower()
    helper_mock.assert_not_awaited()


# ── Missing PDF (r2_pdf_key is null) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_invoice_email_rejects_missing_pdf():
    """Invoice accepted but r2_pdf_key is NULL → 422, no email sent."""
    request = MagicMock()
    helper_mock = AsyncMock(return_value=True)

    with _patch_session(), _patch_db(
        fetchrow=[_order_row(), _invoice_row(r2_pdf_key=None)],
        fetch=[],
    ), patch(
        'app.services.orders_service.send_pos_receipt_email',
        new=helper_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await orders_service.send_invoice_email(request, _ORDER_ID, _RECIPIENT)

    assert exc_info.value.status_code == 422
    assert 'pdf' in exc_info.value.detail.lower()
    helper_mock.assert_not_awaited()


# ── SES failure ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_invoice_email_ses_failure_502():
    """send_pos_receipt_email returns False → 502."""
    request = MagicMock()

    fetchrow = [_order_row(), _invoice_row(), _waro_inferred_row(), _profile_row()]
    fetch = [
        [],
        [],
        [],
        [{'id': uuid4(), 'quantity': 1, 'subtotal': 200.0, 'product_name': 'tomate barranca'}],
        [],
    ]

    with _patch_session(), _patch_db(fetchrow=fetchrow, fetch=fetch), _patch_tax(), patch(
        'app.services.orders_service.send_pos_receipt_email',
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await orders_service.send_invoice_email(request, _ORDER_ID, _RECIPIENT)

    assert exc_info.value.status_code == 502
    assert 'no se pudo' in exc_info.value.detail.lower()
