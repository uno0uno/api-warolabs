"""
Tests for invoice-email endpoints in app.routers.documents (warocol.com#598).

Both endpoints:
  1. Require Module.FACTURACION (already-gated, not tested here — covered by
     existing module-gating tests).
  2. Pull tenant_id from the validated session.
  3. Verify the electronic_invoices.id belongs to that tenant — 404 otherwise.
  4. Proxy to api-facturacion via facturacion_service.proxy_to_facturacion.

Patches:
  - app.routers.documents.require_valid_session → fake session w/ tenant_id
  - app.routers.documents.get_db_connection     → controlled fetchrow result
  - app.services.facturacion_service.httpx.AsyncClient → captures proxy call
"""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.routers import documents as documents_router


_TENANT_ID = UUID('93b3e582-34fa-44a6-8d0f-bf82a3608727')
_TRACK_ID = uuid4()


class _ConnCtx:
    """Minimal async context manager wrapping a single row response."""

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=self._row)
        return conn

    async def __aexit__(self, *_):
        return False


def _patch_db_returns(row):
    return patch(
        'app.routers.documents.get_db_connection',
        lambda *args, **kwargs: _ConnCtx(row),
    )


def _patch_session():
    fake_session = MagicMock()
    fake_session.tenant_id = _TENANT_ID
    return patch(
        'app.routers.documents.require_valid_session',
        return_value=fake_session,
    )


def _patch_httpx(status_code: int = 200, body: dict = None):
    """Patch httpx.AsyncClient where the proxy helper imports it."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=body or {'status': 'sent'})
    resp.text = '{"status":"sent"}'

    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    return patch(
        'app.services.facturacion_service.httpx.AsyncClient',
        return_value=client,
    ), client


# ── resend-email ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resend_email_proxies_to_facturacion():
    """Owned invoice → proxy fires with the right URL, response bubbles."""
    request = MagicMock()
    httpx_patch, fake_client = _patch_httpx(
        body={'status': 'sent', 'matias_response': {'ok': True}}
    )

    with _patch_session(), _patch_db_returns({'id': _TRACK_ID}), httpx_patch:
        result = await documents_router.resend_document_email(request, _TRACK_ID)

    assert result == {'status': 'sent', 'matias_response': {'ok': True}}
    fake_client.post.assert_awaited_once()
    posted_url = fake_client.post.await_args.args[0]
    assert posted_url.endswith(f'/documents/{_TRACK_ID}/resend-email')


@pytest.mark.asyncio
async def test_resend_email_blocks_cross_tenant():
    """Invoice not owned by session tenant → 404, no proxy call."""
    request = MagicMock()
    httpx_patch, fake_client = _patch_httpx()

    with _patch_session(), _patch_db_returns(None), httpx_patch:
        with pytest.raises(HTTPException) as exc_info:
            await documents_router.resend_document_email(request, _TRACK_ID)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == 'Invoice not found'
    fake_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_resend_email_facturacion_503_bubbles():
    """Proxy returns 503 → api-warolabs returns 503 with detail propagated."""
    request = MagicMock()
    httpx_patch, _ = _patch_httpx(
        status_code=503, body={'detail': 'upstream down'},
    )

    with _patch_session(), _patch_db_returns({'id': _TRACK_ID}), httpx_patch:
        with pytest.raises(HTTPException) as exc_info:
            await documents_router.resend_document_email(request, _TRACK_ID)

    assert exc_info.value.status_code == 503


# ── send-email-to ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_email_to_proxies_with_body():
    """Owned invoice + valid email → forwards body {email, name} to facturacion."""
    request = MagicMock()
    httpx_patch, fake_client = _patch_httpx(
        body={'status': 'sent', 'email': 'contador@empresa.com'}
    )
    body = documents_router.SendEmailToBody(
        email='contador@empresa.com', name='Contador'
    )

    with _patch_session(), _patch_db_returns({'id': _TRACK_ID}), httpx_patch:
        result = await documents_router.send_document_email_to(request, _TRACK_ID, body)

    assert result['status'] == 'sent'
    fake_client.post.assert_awaited_once()
    posted_url = fake_client.post.await_args.args[0]
    assert posted_url.endswith(f'/documents/{_TRACK_ID}/send-email-to')

    sent_body = fake_client.post.await_args.kwargs['json']
    assert sent_body == {'email': 'contador@empresa.com', 'name': 'Contador'}


@pytest.mark.asyncio
async def test_send_email_to_blocks_cross_tenant():
    """Invoice not owned by session tenant → 404, no proxy call."""
    request = MagicMock()
    httpx_patch, fake_client = _patch_httpx()
    body = documents_router.SendEmailToBody(email='x@y.com', name=None)

    with _patch_session(), _patch_db_returns(None), httpx_patch:
        with pytest.raises(HTTPException) as exc_info:
            await documents_router.send_document_email_to(request, _TRACK_ID, body)

    assert exc_info.value.status_code == 404
    fake_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_email_to_validates_email_format():
    """Invalid email → pydantic raises before we even enter the handler."""
    # Pydantic v2 raises ValidationError, but FastAPI converts it to a 422
    # on the request boundary. At unit-test level we test the model directly.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        documents_router.SendEmailToBody(email='not-an-email', name=None)


@pytest.mark.asyncio
async def test_send_email_to_empty_name_defaults_to_empty_string():
    """Optional `name` field absent → forwarded as empty string to facturacion."""
    request = MagicMock()
    httpx_patch, fake_client = _patch_httpx()
    body = documents_router.SendEmailToBody(email='x@y.com', name=None)

    with _patch_session(), _patch_db_returns({'id': _TRACK_ID}), httpx_patch:
        await documents_router.send_document_email_to(request, _TRACK_ID, body)

    sent_body = fake_client.post.await_args.kwargs['json']
    assert sent_body == {'email': 'x@y.com', 'name': ''}
