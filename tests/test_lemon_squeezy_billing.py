"""Lemon Squeezy checkout + webhook activation (#942)."""
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
from app.services import billing_service, lemon_squeezy_service


def _sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _created_payload(
    *,
    tenant_id,
    order_id=101,
    subscription_id="55",
    status="active",
    attempt_id=None,
    amount_minor=None,
    include_total=False,
):
    custom = {
        "tenant_id": str(tenant_id),
        "plan_id": str(uuid4()),
        "billing_cycle": "monthly",
        "provider_environment": "test",
    }
    if attempt_id is not None:
        custom["attempt_id"] = str(attempt_id)
    attrs = {
        "status": status,
        "order_id": order_id,
        "currency": "USD",
        "created_at": "2026-08-01T12:00:00Z",
        "first_subscription_item": {
            "price_id": 99,
            "quantity": 1,
        },
    }
    # Real LS subscription_created omits money; tests may inject total for invoice events.
    if include_total and amount_minor is not None:
        attrs["total"] = amount_minor
    return {
        "meta": {
            "event_name": "subscription_created",
            "custom_data": custom,
        },
        "data": {
            "type": "subscriptions",
            "id": subscription_id,
            "attributes": attrs,
        },
    }


def test_verify_lemon_squeezy_signature_ok():
    secret = "whsec_ls_test"
    raw = b'{"meta":{"event_name":"subscription_created"}}'
    with patch.object(
        lemon_squeezy_service.settings, "lemon_squeezy_webhook_secret_sandbox", secret
    ):
        lemon_squeezy_service.verify_lemon_squeezy_signature(
            raw_body=raw,
            signature_header=_sign(raw, secret),
            environment="test",
        )


def test_verify_lemon_squeezy_signature_rejects_bad():
    with patch.object(
        lemon_squeezy_service.settings, "lemon_squeezy_webhook_secret_sandbox", "whsec_ls"
    ):
        with pytest.raises(HTTPException) as exc:
            lemon_squeezy_service.verify_lemon_squeezy_signature(
                raw_body=b"{}",
                signature_header="deadbeef",
                environment="test",
            )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_create_checkout_uses_offer_not_plan_cop(monkeypatch):
    offer = resolve_price_offer("CO")
    monkeypatch.setattr(lemon_squeezy_service.settings, "lemon_squeezy_api_key", None)

    result = await lemon_squeezy_service.create_checkout(
        offer=offer,
        environment="test",
        tenant_id=uuid4(),
        plan_id=uuid4(),
        billing_cycle="monthly",
        redirect_url="https://example.test/billing/confirmacion",
    )

    assert result["currency"] == "USD"
    assert result["amount_minor"] == 900
    assert result["mock"] is True
    assert "ls_checkout=" in result["checkout_url"]


@pytest.mark.asyncio
async def test_create_checkout_posts_variant_and_custom(monkeypatch):
    offer = resolve_price_offer("CO")
    monkeypatch.setattr(
        lemon_squeezy_service.settings, "lemon_squeezy_api_key", "ls_test_key"
    )
    monkeypatch.setattr(
        lemon_squeezy_service.settings, "lemon_squeezy_store_id", "1"
    )
    monkeypatch.setattr(
        lemon_squeezy_service,
        "configured_variant_id",
        lambda _offer, _env: "42",
    )
    monkeypatch.setattr(
        lemon_squeezy_service,
        "require_usable_variant_id",
        lambda variant_id, _env: variant_id,
    )

    captured: dict = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {
                "data": {
                    "id": "chk_1",
                    "attributes": {
                        "url": "https://waro.lemonsqueezy.com/checkout/custom/chk_1",
                    },
                }
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    tenant_id = uuid4()
    plan_id = uuid4()
    with patch("app.services.lemon_squeezy_service.httpx.AsyncClient", _Client):
        result = await lemon_squeezy_service.create_checkout(
            offer=offer,
            environment="test",
            tenant_id=tenant_id,
            plan_id=plan_id,
            billing_cycle="monthly",
            redirect_url="https://warocol.com/billing/confirmacion",
            customer_email="a@b.co",
        )

    assert captured["url"].endswith("/checkouts")
    attrs = captured["json"]["data"]["attributes"]
    assert attrs["checkout_data"]["custom"]["tenant_id"] == str(tenant_id)
    assert attrs["checkout_data"]["email"] == "a@b.co"
    assert attrs["product_options"]["redirect_url"].endswith("/billing/confirmacion")
    assert result["gateway_reference"] == "ls_chk_chk_1"
    assert result["mock"] is False


@pytest.mark.asyncio
async def test_handle_webhook_activates_resolving_order_amount():
    """Real subscription_created has no total — resolve via GET /orders/:id."""
    tenant_id = uuid4()
    payload = _created_payload(tenant_id=tenant_id)  # no total

    mock_conn = MagicMock()

    @asynccontextmanager
    async def _db(*args, **kwargs):
        yield mock_conn

    with (
        patch("app.database.get_db_connection", _db),
        patch(
            "app.services.lemon_squeezy_service.fetch_order_totals",
            new_callable=AsyncMock,
            return_value={"amount_minor": 900, "amount_subtotal_minor": 900, "currency": "USD"},
        ),
        patch(
            "app.services.billing_service.activate_subscription_by_gateway_ref",
            new_callable=AsyncMock,
            return_value=True,
        ) as activate,
        patch(
            "app.services.billing_service.get_tenant_notify_info_after_activate",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await lemon_squeezy_service.handle_verified_webhook(
            payload, environment="test"
        )

    assert result["activated"] is True
    activate.assert_awaited_once()
    kwargs = activate.await_args.kwargs
    assert kwargs["provider"] == "lemon_squeezy"
    assert kwargs["gateway_reference"] == "ls_ord_101"
    assert kwargs["amount"] == 9.0
    assert kwargs["currency"] == "USD"
    assert kwargs["ls_order_id"] == "101"
    assert kwargs["ls_subscription_id"] == "55"


@pytest.mark.asyncio
async def test_handle_webhook_amount_falls_back_to_price_offer():
    tenant_id = uuid4()
    payload = _created_payload(tenant_id=tenant_id)

    mock_conn = MagicMock()

    @asynccontextmanager
    async def _db(*args, **kwargs):
        yield mock_conn

    with (
        patch("app.database.get_db_connection", _db),
        patch(
            "app.services.lemon_squeezy_service.fetch_order_totals",
            new_callable=AsyncMock,
            return_value={"amount_minor": 0, "amount_subtotal_minor": 0, "currency": None},
        ),
        patch(
            "app.services.billing_service.get_tenant_billing_context",
            new_callable=AsyncMock,
            return_value={"slug": "demo", "country_code": "CO"},
        ),
        patch(
            "app.services.billing_service.activate_subscription_by_gateway_ref",
            new_callable=AsyncMock,
            return_value=True,
        ) as activate,
        patch(
            "app.services.billing_service.get_tenant_notify_info_after_activate",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await lemon_squeezy_service.handle_verified_webhook(
            payload, environment="test"
        )

    assert result["activated"] is True
    assert activate.await_args.kwargs["amount"] == 9.0


def test_extract_subscription_payment_success_uses_invoice_ids():
    tenant_id = uuid4()
    payload = {
        "meta": {
            "event_name": "subscription_payment_success",
            "custom_data": {
                "tenant_id": str(tenant_id),
                "plan_id": str(uuid4()),
                "billing_cycle": "monthly",
                "provider_environment": "test",
            },
        },
        "data": {
            "type": "subscription-invoices",
            "id": "646575",
            "attributes": {
                "subscription_id": 207468,
                "billing_reason": "renewal",
                "status": "paid",
                "currency": "USD",
                "subtotal": 900,
                "tax": 180,
                "total": 1080,
                "created_at": "2026-08-01T12:00:00Z",
            },
        },
    }
    parsed = lemon_squeezy_service.extract_subscription_event(payload)
    assert parsed["ls_invoice_id"] == "646575"
    assert parsed["ls_subscription_id"] == "207468"
    assert parsed["gateway_reference"] == "ls_inv_646575"
    assert parsed["amount_minor"] == 1080
    assert parsed["amount_subtotal_minor"] == 900
    assert parsed["billing_reason"] == "renewal"


@pytest.mark.asyncio
async def test_handle_renewal_invoice_activates_with_invoice_ref():
    tenant_id = uuid4()
    payload = {
        "meta": {
            "event_name": "subscription_payment_success",
            "custom_data": {
                "tenant_id": str(tenant_id),
                "plan_id": str(uuid4()),
                "billing_cycle": "monthly",
                "provider_environment": "test",
            },
        },
        "data": {
            "type": "subscription-invoices",
            "id": "9001",
            "attributes": {
                "subscription_id": 55,
                "billing_reason": "renewal",
                "status": "paid",
                "currency": "USD",
                "subtotal": 900,
                "tax": 180,
                "total": 1080,
                "created_at": "2026-08-01T12:00:00Z",
            },
        },
    }

    mock_conn = MagicMock()

    @asynccontextmanager
    async def _db(*args, **kwargs):
        yield mock_conn

    with (
        patch("app.database.get_db_connection", _db),
        patch(
            "app.services.billing_service.activate_subscription_by_gateway_ref",
            new_callable=AsyncMock,
            return_value=True,
        ) as activate,
        patch(
            "app.services.billing_service.get_tenant_notify_info_after_activate",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await lemon_squeezy_service.handle_verified_webhook(
            payload, environment="test"
        )

    assert result["activated"] is True
    kwargs = activate.await_args.kwargs
    assert kwargs["gateway_reference"] == "ls_inv_9001"
    assert kwargs["ls_invoice_id"] == "9001"
    assert kwargs["ls_subscription_id"] == "55"
    assert kwargs["amount"] == 10.80


@pytest.mark.asyncio
async def test_onboarding_accepts_tax_exclusive_total_above_list():
    attempt_id = uuid4()
    tenant_id = uuid4()
    plan_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": attempt_id,
                "tenant_id": tenant_id,
                "plan_id": plan_id,
                "provider_reference": "ls_chk_1",
                "expected_amount_in_cents": 900,
                "currency": "USD",
                "status": "pending",
                "provider_transaction_id": None,
                "provider_environment": "test",
                "plan_name": "Pro",
                "plan_is_active": True,
            },
            {"id": uuid4(), "status": "pending"},
            {
                "id": uuid4(),
                "current_period_end": __import__("datetime").datetime(
                    2026, 9, 1, tzinfo=__import__("datetime").timezone.utc
                ),
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch(
        "app.services.billing_service.onboarding_service.activate_paid_onboarding_identity",
        new_callable=AsyncMock,
        return_value={
            "tenant_name": "T",
            "tenant_email": "t@example.com",
        },
    ):
        result = await billing_service.process_mo_r_onboarding_payment(
            conn,
            attempt_id=attempt_id,
            transaction_id="ls_ord_1",
            amount_minor=1080,
            currency="USD",
            provider_environment="test",
            provider="lemon_squeezy",
            amount_subtotal_minor=900,
            ls_order_id="1",
            ls_subscription_id="55",
        )

    assert result["activated"] is True


def test_lemon_squeezy_payment_matches_expected_matrix():
    assert billing_service.lemon_squeezy_payment_matches_expected(
        expected_amount=900, charged=1080, subtotal=900
    )
    assert billing_service.lemon_squeezy_payment_matches_expected(
        expected_amount=900, charged=900, subtotal=756
    )
    assert not billing_service.lemon_squeezy_payment_matches_expected(
        expected_amount=900, charged=800, subtotal=756
    )
    assert not billing_service.lemon_squeezy_payment_matches_expected(
        expected_amount=900, charged=950, subtotal=756
    )
    assert not billing_service.lemon_squeezy_payment_matches_expected(
        expected_amount=900, charged=1080, subtotal=1000
    )
    assert billing_service.lemon_squeezy_payment_matches_expected(
        expected_amount=900, charged=1080, subtotal=None
    )


@pytest.mark.asyncio
async def test_onboarding_accepts_tax_inclusive_list_price_as_total():
    attempt_id = uuid4()
    tenant_id = uuid4()
    plan_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": attempt_id,
                "tenant_id": tenant_id,
                "plan_id": plan_id,
                "provider_reference": "ls_chk_1",
                "expected_amount_in_cents": 900,
                "currency": "USD",
                "status": "pending",
                "provider_transaction_id": None,
                "provider_environment": "test",
                "plan_name": "Pro",
                "plan_is_active": True,
            },
            {"id": uuid4(), "status": "pending"},
            {
                "id": uuid4(),
                "current_period_end": __import__("datetime").datetime(
                    2026, 9, 1, tzinfo=__import__("datetime").timezone.utc
                ),
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch(
        "app.services.billing_service.onboarding_service.activate_paid_onboarding_identity",
        new_callable=AsyncMock,
        return_value={
            "tenant_name": "T",
            "tenant_email": "t@example.com",
        },
    ):
        result = await billing_service.process_mo_r_onboarding_payment(
            conn,
            attempt_id=attempt_id,
            transaction_id="ls_ord_2",
            amount_minor=900,
            currency="USD",
            provider_environment="test",
            provider="lemon_squeezy",
            amount_subtotal_minor=756,
            ls_order_id="2",
            ls_subscription_id="55",
        )

    assert result["activated"] is True


@pytest.mark.asyncio
async def test_onboarding_rejects_subtotal_above_list():
    attempt_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": attempt_id,
            "tenant_id": uuid4(),
            "plan_id": uuid4(),
            "provider_reference": "ls_chk_1",
            "expected_amount_in_cents": 900,
            "currency": "USD",
            "status": "pending",
            "provider_transaction_id": None,
            "provider_environment": "test",
            "plan_name": "Pro",
            "plan_is_active": True,
        }
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await billing_service.process_mo_r_onboarding_payment(
            conn,
            attempt_id=attempt_id,
            transaction_id="ls_ord_bad",
            amount_minor=1080,
            currency="USD",
            provider_environment="test",
            provider="lemon_squeezy",
            amount_subtotal_minor=1000,
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_handle_renewal_invoice_tax_inclusive_activates():
    tenant_id = uuid4()
    payload = {
        "meta": {
            "event_name": "subscription_payment_success",
            "custom_data": {
                "tenant_id": str(tenant_id),
                "plan_id": str(uuid4()),
                "billing_cycle": "monthly",
                "provider_environment": "test",
            },
        },
        "data": {
            "type": "subscription-invoices",
            "id": "9002",
            "attributes": {
                "subscription_id": 55,
                "billing_reason": "renewal",
                "status": "paid",
                "currency": "USD",
                "subtotal": 756,
                "tax": 144,
                "total": 900,
                "created_at": "2026-08-01T12:00:00Z",
            },
        },
    }

    mock_conn = MagicMock()

    @asynccontextmanager
    async def _db(*args, **kwargs):
        yield mock_conn

    with (
        patch("app.database.get_db_connection", _db),
        patch(
            "app.services.billing_service.activate_subscription_by_gateway_ref",
            new_callable=AsyncMock,
            return_value=True,
        ) as activate,
        patch(
            "app.services.billing_service.get_tenant_notify_info_after_activate",
            new_callable=AsyncMock,
            return_value={"tenant_email": "t@example.com"},
        ),
    ):
        result = await lemon_squeezy_service.handle_verified_webhook(
            payload, environment="test"
        )

    assert result["activated"] is True
    assert activate.await_args.kwargs["amount"] == 9.0


@pytest.mark.asyncio
async def test_handle_webhook_ignores_noise_events():
    payload = _created_payload(tenant_id=uuid4())
    payload["meta"]["event_name"] = "license_key_created"
    result = await lemon_squeezy_service.handle_verified_webhook(
        payload, environment="test"
    )
    assert result["activated"] is False
    assert result["reason"] == "ignored_event"


@pytest.mark.asyncio
async def test_handle_webhook_payment_failed_marks_past_due():
    tenant_id = uuid4()
    payload = _created_payload(tenant_id=tenant_id)
    payload["meta"]["event_name"] = "subscription_payment_failed"

    mock_conn = MagicMock()

    @asynccontextmanager
    async def _db(*args, **kwargs):
        yield mock_conn

    with (
        patch("app.database.get_db_connection", _db),
        patch(
            "app.services.billing_service.mark_subscription_past_due_by_tenant",
            new_callable=AsyncMock,
            return_value={"tenant_email": None},
        ) as past_due,
    ):
        result = await lemon_squeezy_service.handle_verified_webhook(
            payload, environment="test"
        )

    assert result["activated"] is False
    assert result["reason"] == "failed_or_cancelled"
    past_due.assert_awaited_once()
    assert past_due.await_args.kwargs["provider"] == "lemon_squeezy"


def test_lemon_squeezy_sandbox_webhook_route_verifies_signature():
    app = FastAPI()
    app.include_router(payments_webhook_router)
    client = TestClient(app)
    secret = "sandbox_secret"
    payload = _created_payload(tenant_id=uuid4())
    raw = json.dumps(payload).encode("utf-8")

    with (
        patch.object(
            lemon_squeezy_service.settings,
            "lemon_squeezy_webhook_secret_sandbox",
            secret,
        ),
        patch.object(
            lemon_squeezy_service,
            "handle_verified_webhook",
            new_callable=AsyncMock,
            return_value={"ok": True, "activated": False},
        ) as handle,
    ):
        res = client.post(
            "/payments/webhooks/lemon-squeezy/sandbox",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sign(raw, secret),
            },
        )

    assert res.status_code == 200
    handle.assert_awaited_once()
