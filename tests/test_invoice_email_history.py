"""
Tests for invoice email delivery history + tracking pixel (api-warolabs#657).

Covers:
  - Token hashing: only SHA-256 hex is persisted, never the raw token.
  - Lifecycle: pending insert → sent/failed finalization.
  - record_pixel_open: hash-only lookup, silent on DB errors (never breaks SES flow).
  - list_deliveries_for_order: always filtered by tenant_id + order_id.
  - Pixel endpoint: identical 200 + 1x1 GIF + no-store for valid/invalid tokens.
  - email-history endpoint: tenant_id comes from the session, not the caller.
  - MIME: multipart/alternative (text+html) with attachments; plain text without html_body.

Patches:
  - app.services.invoice_email_tracking_service.get_db_connection → fake conn
  - app.routers.orders.require_valid_session → fake session w/ tenant_id
"""
from email.parser import Parser
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.routers import orders as orders_router
from app.services import invoice_email_tracking_service as svc
from app.services.aws_ses_service import AWSSESService
from app.services.email_helpers import _build_receipt_html_with_pixel


_TENANT_ID = uuid4()
_ORDER_ID = uuid4()
_DELIVERY_ID = uuid4()


class _Ctx:
    """Minimal async context manager wrapping a fake connection."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return False


def _patch_db(conn):
    return patch.object(svc, "get_db_connection", lambda *a, **k: _Ctx(conn))


# ── Token hashing ─────────────────────────────────────────────────────────────


def test_hash_tracking_token_is_sha256_hex_not_raw():
    token = svc.generate_tracking_token()
    digest = svc.hash_tracking_token(token)
    assert digest != token
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert svc.hash_tracking_token(token) == digest


# ── Persistence lifecycle ─────────────────────────────────────────────────────


async def test_create_pending_delivery_inserts_pending_row():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": _DELIVERY_ID})

    with _patch_db(conn):
        result = await svc.create_pending_delivery(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            recipient_email="cliente@example.com",
            tracking_token_hash="ab" * 32,
        )

    assert result == _DELIVERY_ID
    sql, *params = conn.fetchrow.call_args.args
    assert "INSERT INTO invoice_email_deliveries" in sql
    assert "'pending'" in sql
    assert params == [_TENANT_ID, _ORDER_ID, "cliente@example.com", "ab" * 32]


async def test_mark_delivery_sent_sets_sent_status():
    conn = MagicMock()
    conn.execute = AsyncMock()

    with _patch_db(conn):
        await svc.mark_delivery_sent(_DELIVERY_ID)

    sql, delivery_id = conn.execute.call_args.args
    assert "status = 'sent'" in sql
    assert delivery_id == _DELIVERY_ID


async def test_mark_delivery_failed_truncates_failure_code():
    conn = MagicMock()
    conn.execute = AsyncMock()

    with _patch_db(conn):
        await svc.mark_delivery_failed(_DELIVERY_ID, failure_code="x" * 200)

    sql, delivery_id, code = conn.execute.call_args.args
    assert "status = 'failed'" in sql
    assert delivery_id == _DELIVERY_ID
    assert len(code) == 120


async def test_mark_delivery_failed_empty_code_becomes_none():
    conn = MagicMock()
    conn.execute = AsyncMock()

    with _patch_db(conn):
        await svc.mark_delivery_failed(_DELIVERY_ID, failure_code=None)

    assert conn.execute.call_args.args[2] is None


async def test_mark_delivery_sent_swallows_db_errors():
    """SES already accepted — a DB failure must not propagate (no resend, no 502)."""
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with _patch_db(conn):
        await svc.mark_delivery_sent(_DELIVERY_ID)  # must not raise


# ── Pixel open recording ──────────────────────────────────────────────────────


async def test_record_pixel_open_updates_by_hash_only():
    conn = MagicMock()
    conn.execute = AsyncMock()

    with _patch_db(conn):
        await svc.record_pixel_open("cd" * 32)

    sql, token_hash = conn.execute.call_args.args
    assert "WHERE tracking_token_hash = $1" in sql
    assert "open_count = open_count + 1" in sql
    assert token_hash == "cd" * 32


async def test_record_pixel_open_is_silent_on_db_error():
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with _patch_db(conn):
        await svc.record_pixel_open("cd" * 32)  # must not raise


# ── Tenant isolation ──────────────────────────────────────────────────────────


async def test_list_deliveries_for_order_filters_tenant_and_order():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])

    with _patch_db(conn):
        result = await svc.list_deliveries_for_order(
            tenant_id=_TENANT_ID, order_id=_ORDER_ID
        )

    assert result == []
    sql, tenant_id, order_id = conn.fetch.call_args.args
    assert "tenant_id = $1" in sql
    assert "order_id = $2" in sql
    assert tenant_id == _TENANT_ID
    assert order_id == _ORDER_ID


async def test_email_history_endpoint_uses_session_tenant():
    fake_session = MagicMock()
    fake_session.tenant_id = _TENANT_ID
    expected = [{"id": str(_DELIVERY_ID), "status": "sent"}]

    with patch.object(
        orders_router, "require_valid_session", return_value=fake_session
    ), patch.object(
        svc, "list_deliveries_for_order", new=AsyncMock(return_value=expected)
    ) as list_mock:
        result = await orders_router.get_order_invoice_email_history(
            request=MagicMock(), order_id=_ORDER_ID
        )

    assert result == {"deliveries": expected}
    assert list_mock.call_args.kwargs["tenant_id"] == _TENANT_ID
    assert list_mock.call_args.kwargs["order_id"] == _ORDER_ID


# ── Pixel endpoint parity (valid vs invalid token) ────────────────────────────


async def test_pixel_endpoint_identical_response_for_unknown_token(client):
    conn = MagicMock()
    conn.execute = AsyncMock()

    known_token = svc.generate_tracking_token()
    unknown_token = svc.generate_tracking_token()

    with _patch_db(conn):
        resp_known = await client.get(f"/public/email-tracking/{known_token}.gif")
        resp_unknown = await client.get(f"/public/email-tracking/{unknown_token}.gif")

    assert resp_known.status_code == 200
    assert resp_unknown.status_code == 200
    assert resp_known.content == svc.PIXEL_GIF_BYTES
    assert resp_unknown.content == svc.PIXEL_GIF_BYTES
    for resp in (resp_known, resp_unknown):
        assert resp.headers["content-type"] == "image/gif"
        assert "no-store" in resp.headers["cache-control"]

    # The DB lookup always receives the SHA-256 hash, never the raw token.
    for call in conn.execute.call_args_list:
        token_hash = call.args[1]
        assert token_hash not in (known_token, unknown_token)
        assert len(token_hash) == 64


# ── HTML body + pixel ─────────────────────────────────────────────────────────


def test_receipt_html_is_none_without_pixel():
    assert _build_receipt_html_with_pixel("Hola", None) is None


def test_receipt_html_embeds_pixel_and_escapes_text():
    html = _build_receipt_html_with_pixel(
        "Total <b>$10</b>", "https://api.example.com/public/email-tracking/tok.gif"
    )
    assert 'src="https://api.example.com/public/email-tracking/tok.gif"' in html
    assert "Total &lt;b&gt;$10&lt;/b&gt;" in html
    assert "<b>$10</b>" not in html


# ── MIME structure ────────────────────────────────────────────────────────────


def _ses_service_with_mock_client():
    service = AWSSESService.__new__(AWSSESService)
    service.client = MagicMock()
    service.client.send_raw_email = MagicMock(return_value={"MessageId": "msg-1"})
    return service


def _sent_mime(service) -> dict:
    raw = service.client.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
    return Parser().parsestr(raw)


async def test_send_email_with_attachments_wraps_html_in_alternative():
    service = _ses_service_with_mock_client()

    ok = await service.send_email_with_attachments(
        from_email="facturas@warocol.com",
        to_emails=["cliente@example.com"],
        subject="Factura",
        text_body="texto plano",
        html_body="<html><body><p>html</p><img src=\"pixel.gif\"/></body></html>",
        attachments=[{"data": b"%PDF-1.4", "filename": "factura.pdf", "content_type": "application/pdf"}],
    )

    assert ok is True
    msg = _sent_mime(service)
    assert msg.get_content_type() == "multipart/mixed"
    types = [part.get_content_type() for part in msg.get_payload()]
    assert "multipart/alternative" in types
    attachment = next(p for p in msg.get_payload() if p.get_filename() == "factura.pdf")
    assert "application/pdf" in attachment.get_all("Content-Type")
    alternative = next(p for p in msg.get_payload() if p.get_content_type() == "multipart/alternative")
    alt_types = [p.get_content_type() for p in alternative.get_payload()]
    assert alt_types == ["text/plain", "text/html"]


async def test_send_email_with_attachments_text_only_without_html():
    service = _ses_service_with_mock_client()

    ok = await service.send_email_with_attachments(
        from_email="facturas@warocol.com",
        to_emails=["cliente@example.com"],
        subject="Factura",
        text_body="texto plano",
        attachments=[{"data": b"%PDF-1.4", "filename": "factura.pdf", "content_type": "application/pdf"}],
    )

    assert ok is True
    msg = _sent_mime(service)
    types = [part.get_content_type() for part in msg.get_payload()]
    assert "multipart/alternative" not in types
    assert "text/plain" in types
    assert any(p.get_filename() == "factura.pdf" for p in msg.get_payload())
