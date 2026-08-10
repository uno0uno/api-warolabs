"""Paddle checkout + webhook activation (#795)."""
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Optional
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.billing_pricing import resolve_price_offer
from app.routers.payments_webhook import router as payments_webhook_router
from app.services import billing_service, paddle_service


def _sign(raw: bytes, secret: str, ts: Optional[str] = None) -> str:
    if ts is None:
        ts = str(int(time.time()))
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{ts}:".encode("utf-8") + raw,
        hashlib.sha256,
    ).hexdigest()
    return f"ts={ts};h1={digest}"


def _completed_payload(
    *,
    tenant_id,
    txn_id="txn_paddle_1",
    status="completed",
    attempt_id=None,
):
    custom = {
        "tenant_id": str(tenant_id),
        "plan_id": str(uuid4()),
        "billing_cycle": "annual",
        "provider_environment": "test",
    }
    if attempt_id is not None:
        custom["attempt_id"] = str(attempt_id)
    return {
        "event_type": "transaction.completed",
        "data": {
            "id": txn_id,
            "status": status,
            "currency_code": "USD",
            "billed_at": "2026-08-01T12:00:00Z",
            "custom_data": custom,
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


def test_verify_paddle_signature_rejects_stale_ts():
    secret = "whsec_test"
    raw = b"{}"
    with patch.object(paddle_service.settings, "paddle_webhook_secret_sandbox", secret):
        with pytest.raises(HTTPException) as exc:
            paddle_service.verify_paddle_signature(
                raw_body=raw,
                signature_header=_sign(raw, secret, ts="1"),
                environment="test",
            )
    assert exc.value.status_code == 401
    assert "skew" in exc.value.detail.lower()


def test_verify_paddle_signature_rejects_bad_h1():
    with patch.object(paddle_service.settings, "paddle_webhook_secret_sandbox", "whsec_test"):
        with pytest.raises(HTTPException) as exc:
            paddle_service.verify_paddle_signature(
                raw_body=b"{}",
                signature_header=f"ts={int(time.time())};h1=deadbeef",
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
    activate = AsyncMock(return_value=True)
    notify_info = AsyncMock(
        return_value={
            "tenant_id": str(tenant_id),
            "subscription_id": str(uuid4()),
            "tenant_name": "T",
            "tenant_email": "t@example.com",
            "plan_name": "Pro",
            "next_period_end": "2027-01-01T00:00:00+00:00",
        }
    )
    background = BackgroundTasks()

    @asynccontextmanager
    async def _db():
        yield MagicMock()

    with patch("app.database.get_db_connection", side_effect=_db), patch(
        "app.services.billing_service.activate_subscription_by_gateway_ref",
        activate,
    ), patch(
        "app.services.billing_service.get_tenant_notify_info_after_activate",
        notify_info,
    ):
        out = await paddle_service.handle_verified_webhook(
            payload, environment="test", background_tasks=background
        )

    assert out["activated"] is True
    activate.assert_awaited_once()
    assert len(background.tasks) == 1
    assert background.tasks[0].func is paddle_service._notify_payment_approved
    kwargs = activate.await_args.kwargs
    assert kwargs["provider"] == "paddle"
    assert kwargs["paddle_transaction_id"] == "txn_paddle_1"
    assert kwargs["paddle_subscription_id"] == "sub_paddle_1"
    assert kwargs["currency"] == "USD"
    assert kwargs["amount"] == 90.0


@pytest.mark.asyncio
async def test_handle_webhook_noop_reports_not_activated():
    tenant_id = uuid4()
    payload = _completed_payload(tenant_id=tenant_id)
    activate = AsyncMock(return_value=False)

    @asynccontextmanager
    async def _db():
        yield MagicMock()

    with patch("app.database.get_db_connection", side_effect=_db), patch(
        "app.services.billing_service.activate_subscription_by_gateway_ref",
        activate,
    ):
        out = await paddle_service.handle_verified_webhook(payload, environment="test")

    assert out["activated"] is False
    assert out["reason"] == "not_activated"


@pytest.mark.asyncio
async def test_handle_webhook_routes_onboarding_attempt():
    tenant_id = uuid4()
    attempt_id = uuid4()
    payload = _completed_payload(tenant_id=tenant_id, attempt_id=attempt_id)
    onboard = AsyncMock(
        return_value={
            "handled": True,
            "activated": True,
            "tenant_info": {
                "tenant_id": str(tenant_id),
                "subscription_id": str(uuid4()),
                "tenant_name": "T",
                "tenant_email": "t@example.com",
                "plan_name": "Pro",
                "next_period_end": "2027-01-01T00:00:00+00:00",
            },
        }
    )
    background = BackgroundTasks()

    @asynccontextmanager
    async def _db():
        yield MagicMock()

    with patch("app.database.get_db_connection", side_effect=_db), patch(
        "app.services.billing_service.process_paddle_onboarding_payment",
        onboard,
    ), patch(
        "app.services.billing_service.activate_subscription_by_gateway_ref",
        new=AsyncMock(),
    ) as activate:
        out = await paddle_service.handle_verified_webhook(
            payload, environment="test", background_tasks=background
        )

    assert out["activated"] is True
    assert out["onboarding"] is True
    onboard.assert_awaited_once()
    assert onboard.await_args.kwargs["attempt_id"] == attempt_id
    activate.assert_not_awaited()
    # First onboarding payment must not queue "renovada" email
    assert len(background.tasks) == 0


@pytest.mark.asyncio
async def test_subscription_activated_event_ignored():
    payload = {
        "event_type": "subscription.activated",
        "data": {
            "id": "sub_only",
            "status": "active",
            "custom_data": {"tenant_id": str(uuid4())},
        },
    }
    activate = AsyncMock()
    with patch("app.services.billing_service.activate_subscription_by_gateway_ref", activate):
        out = await paddle_service.handle_verified_webhook(payload, environment="test")

    assert out["activated"] is False
    assert out["reason"] == "ignored_event"
    activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_webhook_failed_marks_past_due_by_tenant():
    tenant_id = uuid4()
    payload = {
        "event_type": "transaction.payment_failed",
        "data": {
            "id": "txn_fail",
            "status": "failed",
            "subscription_id": "sub_paddle_fail",
            "custom_data": {"tenant_id": str(tenant_id)},
        },
    }
    past_due = AsyncMock(
        return_value={
            "tenant_id": str(tenant_id),
            "subscription_id": str(uuid4()),
            "tenant_name": "T",
            "tenant_email": "t@example.com",
        }
    )
    activate = AsyncMock()
    background = BackgroundTasks()

    @asynccontextmanager
    async def _db():
        yield MagicMock()

    with patch("app.database.get_db_connection", side_effect=_db), patch(
        "app.services.billing_service.mark_subscription_past_due_by_tenant",
        past_due,
    ), patch(
        "app.services.billing_service.activate_subscription_by_gateway_ref",
        activate,
    ):
        out = await paddle_service.handle_verified_webhook(
            payload, environment="test", background_tasks=background
        )

    assert out["activated"] is False
    assert out["reason"] == "failed_or_cancelled"
    activate.assert_not_awaited()
    past_due.assert_awaited_once()
    kwargs = past_due.await_args.kwargs
    assert past_due.await_args.args[1] == tenant_id
    assert kwargs["paddle_transaction_id"] == "txn_fail"
    assert kwargs["paddle_subscription_id"] == "sub_paddle_fail"
    assert len(background.tasks) == 1
    assert background.tasks[0].func is paddle_service._notify_payment_rejected


@pytest.mark.asyncio
async def test_activate_paddle_duplicate_skips():
    tenant_id = uuid4()
    sub_row = {"id": uuid4(), "status": "pending", "billing_cycle": "annual"}
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=sub_row)
    conn.fetchval = AsyncMock(return_value=1)  # duplicate paddle txn
    conn.execute = AsyncMock()

    activated = await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="txn_paddle_1",
        amount=90.0,
        currency="USD",
        paddle_transaction_id="txn_paddle_1",
        provider="paddle",
        provider_environment="test",
    )

    assert activated is False
    conn.execute.assert_not_called()
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_paddle_onboarding_payment_activates():
    attempt_id = uuid4()
    tenant_id = uuid4()
    plan_id = uuid4()
    attempt = {
        "id": attempt_id,
        "tenant_id": tenant_id,
        "plan_id": plan_id,
        "provider_reference": "txn_onboard_1",
        "expected_amount_in_cents": 9000,
        "currency": "USD",
        "status": "pending",
        "provider_transaction_id": None,
        "provider_environment": "test",
        "plan_name": "Pro",
        "plan_is_active": True,
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            attempt,
            {"id": uuid4(), "status": "pending"},
            {"id": uuid4(), "current_period_end": datetime(2027, 8, 1, tzinfo=timezone.utc)},
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch(
        "app.services.billing_service.onboarding_service.activate_paid_onboarding_identity",
        new=AsyncMock(return_value={"tenant_name": "Cafe", "tenant_email": "a@b.com"}),
    ):
        result = await billing_service.process_paddle_onboarding_payment(
            conn,
            attempt_id=attempt_id,
            transaction_id="txn_onboard_1",
            amount_minor=9000,
            currency="USD",
            provider_environment="test",
            paddle_subscription_id="sub_1",
        )

    assert result["handled"] is True
    assert result["activated"] is True
    assert result["tenant_info"]["tenant_name"] == "Cafe"


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
