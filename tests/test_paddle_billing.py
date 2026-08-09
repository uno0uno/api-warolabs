"""Paddle checkout + webhook activation (#795)."""
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.billing_pricing import resolve_price_offer
from app.routers.payments_webhook import router as payments_webhook_router
from app.services import billing_service, paddle_service


def _sign(raw: bytes, secret: str, ts: str = "1717200000") -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{ts}:".encode("utf-8") + raw,
        hashlib.sha256,
    ).hexdigest()
    return f"ts={ts};h1={digest}"


def _completed_payload(*, tenant_id, txn_id="txn_paddle_1", status="completed"):
    return {
        "event_type": "transaction.completed",
        "data": {
            "id": txn_id,
            "status": status,
            "currency_code": "USD",
            "billed_at": "2026-08-01T12:00:00Z",
            "custom_data": {
                "tenant_id": str(tenant_id),
                "plan_id": str(uuid4()),
                "billing_cycle": "annual",
                "provider_environment": "test",
            },
            "details": {"totals": {"grand_total": "9000", "currency_code": "USD"}},
            "subscription_id": "sub_paddle_1",
        },
    }


def test_verify_paddle_signature_ok():
    secret = "whsec_test"
    raw = b'{"event_type":"transaction.completed"}'
    with patch.object(paddle_service.settings, "paddle_webhook_secret_sandbox", secret):
        paddle_service.verify_paddle_signature(
            raw_body=raw,
            signature_header=_sign(raw, secret),
            environment="test",
        )


def test_verify_paddle_signature_rejects_bad_h1():
    with patch.object(paddle_service.settings, "paddle_webhook_secret_sandbox", "whsec_test"):
        with pytest.raises(HTTPException) as exc:
            paddle_service.verify_paddle_signature(
                raw_body=b"{}",
                signature_header="ts=1;h1=deadbeef",
                environment="test",
            )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_create_checkout_uses_offer_not_plan_cop(monkeypatch):
    offer = resolve_price_offer("CO")
    monkeypatch.setattr(paddle_service.settings, "paddle_api_key_sandbox", None)

    result = await paddle_service.create_checkout(
        offer=offer,
        environment="test",
        tenant_id=uuid4(),
        plan_id=uuid4(),
        billing_cycle="annual",
        redirect_url="https://example.test/billing/confirmacion",
    )

    assert result["currency"] == "USD"
    assert result["amount_minor"] == 9000  # $90 annual, not COP plan price
    assert result["mock"] is True
    assert "paddle_txn=" in result["checkout_url"]


@pytest.mark.asyncio
async def test_handle_webhook_activates_on_completed():
    tenant_id = uuid4()
    payload = _completed_payload(tenant_id=tenant_id)
    activate = AsyncMock()

    @asynccontextmanager
    async def _db():
        yield MagicMock()

    with patch("app.database.get_db_connection", side_effect=_db), patch(
        "app.services.billing_service.activate_subscription_by_gateway_ref",
        activate,
    ):
        out = await paddle_service.handle_verified_webhook(payload, environment="test")

    assert out["activated"] is True
    activate.assert_awaited_once()
    kwargs = activate.await_args.kwargs
    assert kwargs["provider"] == "paddle"
    assert kwargs["paddle_transaction_id"] == "txn_paddle_1"
    assert kwargs["paddle_subscription_id"] == "sub_paddle_1"
    assert kwargs["currency"] == "USD"
    assert kwargs["amount"] == 90.0


@pytest.mark.asyncio
async def test_handle_webhook_failed_does_not_activate():
    tenant_id = uuid4()
    payload = {
        "event_type": "transaction.payment_failed",
        "data": {
            "id": "txn_fail",
            "status": "failed",
            "custom_data": {"tenant_id": str(tenant_id)},
        },
    }
    activate = AsyncMock()
    with patch("app.services.billing_service.activate_subscription_by_gateway_ref", activate):
        out = await paddle_service.handle_verified_webhook(payload, environment="test")

    assert out["activated"] is False
    assert out["reason"] == "failed_or_cancelled"
    activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_activate_paddle_duplicate_skips():
    tenant_id = uuid4()
    sub_row = {"id": uuid4(), "status": "pending", "billing_cycle": "annual"}
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=sub_row)
    conn.fetchval = AsyncMock(return_value=1)  # duplicate paddle txn
    conn.execute = AsyncMock()

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="txn_paddle_1",
        amount=90.0,
        currency="USD",
        paddle_transaction_id="txn_paddle_1",
        provider="paddle",
        provider_environment="test",
    )

    conn.execute.assert_not_called()
    conn.fetchval.assert_awaited_once()


def test_paddle_webhook_route_verifies_and_handles():
    secret = "whsec_live"
    tenant_id = uuid4()
    payload = _completed_payload(tenant_id=tenant_id)
    raw = json.dumps(payload).encode("utf-8")

    app = FastAPI()
    app.include_router(payments_webhook_router)

    with patch.object(paddle_service.settings, "paddle_webhook_secret_live", secret), patch(
        "app.services.paddle_service.handle_verified_webhook",
        new=AsyncMock(return_value={"ok": True, "activated": True}),
    ) as handler:
        client = TestClient(app)
        res = client.post(
            "/payments/webhooks/paddle",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "Paddle-Signature": _sign(raw, secret),
            },
        )

    assert res.status_code == 200
    assert res.json()["activated"] is True
    handler.assert_awaited_once()
