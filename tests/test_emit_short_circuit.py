"""
Tests for the time-bounded "ya validado" short-circuit in
app.services.facturacion_service.emit_invoice (warocol.com#596).

emit_invoice opens two DB connections in sequence:
  1. order existence/status check
  2. latest electronic_invoices lookup

We patch `get_db_connection` to return a sequenced ConnCtx that yields the
right row on each call, then assert on either the raised HTTPException or
the fact that httpx.AsyncClient.post was invoked (i.e. the request fell
through to the api-facturacion proxy).
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.services import facturacion_service


_TENANT_ID = '93b3e582-34fa-44a6-8d0f-bf82a3608727'
_ORDER_ID = str(uuid4())


class _SeqConnCtx:
    """Context manager that returns a different `conn.fetchrow` row each call.

    `rows` is iterated lazily as the production code calls `get_db_connection`
    repeatedly. Each `__aenter__` returns a fresh fake conn pre-configured to
    return the next row.
    """

    def __init__(self, rows):
        self._iter = iter(rows)

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=next(self._iter))
        return conn

    async def __aexit__(self, *_):
        return False


def _patch_db(rows):
    """Patch `get_db_connection` to yield the supplied rows in order."""
    ctx = _SeqConnCtx(rows)
    return patch(
        'app.services.facturacion_service.get_db_connection',
        lambda *args, **kwargs: ctx,
    )


def _order_row(
    status: str = 'completed',
    payment_method: str = 'cash',
    payment_status: str = 'paid',
    active_payment_count: int = 0,
):
    return {
        'id': UUID(_ORDER_ID),
        'status': status,
        'payment_method': payment_method,
        'payment_status': payment_status,
        'active_payment_count': active_payment_count,
    }


def _latest_invoice_row(
    status: str = 'rejected',
    error_message: str = 'Matias API 400: ya se encuentra validado.',
    created_at: datetime = None,
    invoice_number: int = 5462,
    prefix: str = 'LZT',
):
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return {
        'status':         status,
        'invoice_number': invoice_number,
        'prefix':         prefix,
        'error_message':  error_message,
        'created_at':     created_at,
    }


@pytest.mark.asyncio
async def test_recent_rejected_still_409():
    """`created_at` 60s ago → still inside grace window → 409 with new copy."""
    rows = [
        _order_row(),
        _latest_invoice_row(
            created_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        ),
    ]
    with _patch_db(rows):
        with pytest.raises(HTTPException) as exc_info:
            await facturacion_service.emit_invoice(_ORDER_ID, _TENANT_ID, 'pos')

    assert exc_info.value.status_code == 409
    # New copy — must not still mention "Contacta soporte" or "no se pudo descargar".
    assert 'acaba de' in exc_info.value.detail.lower()
    assert 'esperá' in exc_info.value.detail.lower() or 'espera' in exc_info.value.detail.lower()
    assert 'contacta soporte' not in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_old_rejected_falls_through_to_proxy():
    """`created_at` 10min ago → outside grace window → request reaches api-facturacion."""
    rows = [
        _order_row(),
        _latest_invoice_row(
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        ),
    ]

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.is_success = True
    fake_resp.json = MagicMock(return_value={'status': 'accepted', 'cufe': 'CUFE_OK'})
    fake_resp.text = '{"status":"accepted"}'

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with _patch_db(rows), patch(
        'app.services.facturacion_service.httpx.AsyncClient',
        return_value=fake_client,
    ):
        result = await facturacion_service.emit_invoice(_ORDER_ID, _TENANT_ID, 'pos')

    # Request escaped the gate and hit the proxy with the expected payload.
    fake_client.post.assert_awaited_once()
    posted_url = fake_client.post.await_args.args[0]
    assert posted_url.endswith('/invoice/emit')
    assert result == {'status': 'accepted', 'cufe': 'CUFE_OK'}


@pytest.mark.asyncio
async def test_other_rejection_falls_through_regardless_of_age():
    """Recent rejection with a *non-ya-validado* reason still falls through."""
    rows = [
        _order_row(),
        _latest_invoice_row(
            error_message='Matias API 400: Falta NIT del cliente',
            created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        ),
    ]

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.is_success = True
    fake_resp.json = MagicMock(return_value={'status': 'accepted'})
    fake_resp.text = '{"status":"accepted"}'
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with _patch_db(rows), patch(
        'app.services.facturacion_service.httpx.AsyncClient',
        return_value=fake_client,
    ):
        await facturacion_service.emit_invoice(_ORDER_ID, _TENANT_ID, 'pos')

    fake_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_accepted_short_circuits_to_existing_invoice():
    """A previously accepted invoice → returns it without calling api-facturacion."""
    rows = [
        _order_row(),
        _latest_invoice_row(
            status='accepted',
            error_message=None,
        ),
    ]

    existing_payload = {'invoice_number': 5460, 'cufe': 'CUFE_PRE', 'status': 'accepted'}

    fake_client = MagicMock()
    fake_client.post = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with _patch_db(rows), patch(
        'app.services.facturacion_service.get_order_invoice',
        new=AsyncMock(return_value=existing_payload),
    ), patch(
        'app.services.facturacion_service.httpx.AsyncClient',
        return_value=fake_client,
    ):
        result = await facturacion_service.emit_invoice(_ORDER_ID, _TENANT_ID, 'pos')

    assert result == existing_payload
    fake_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_credit_only_order_does_not_reach_proxy():
    """Pure credit orders are blocked before api-facturacion can return Matias 422."""
    rows = [
        _order_row(payment_method='credit', payment_status='credit', active_payment_count=0),
        None,
    ]

    fake_client = MagicMock()
    fake_client.post = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with _patch_db(rows), patch(
        'app.services.facturacion_service.httpx.AsyncClient',
        return_value=fake_client,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await facturacion_service.emit_invoice(_ORDER_ID, _TENANT_ID, 'pos')

    assert exc_info.value.status_code == 422
    assert 'solo crédito' in exc_info.value.detail
    fake_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_split_credit_order_falls_through_to_proxy():
    """Credit as part of split tender remains invoice-eligible."""
    rows = [
        _order_row(payment_method='credit', payment_status='paid', active_payment_count=1),
        None,
    ]

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.is_success = True
    fake_resp.json = MagicMock(return_value={'status': 'accepted'})
    fake_resp.text = '{"status":"accepted"}'
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with _patch_db(rows), patch(
        'app.services.facturacion_service.httpx.AsyncClient',
        return_value=fake_client,
    ):
        await facturacion_service.emit_invoice(_ORDER_ID, _TENANT_ID, 'pos')

    fake_client.post.assert_awaited_once()
