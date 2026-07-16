"""Wompi central ingress classification and dispatch (#353, #355)."""
import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers.payments_webhook import router as payments_webhook_router
from app.services.wompi_webhook_router_service import (
    WompiRoute,
    classify_transaction_updated,
    dispatch_verified_event,
)

TX_UPDATED = "transaction.updated"


def _transaction_body(environment="prod", **tx_fields):
    return {
        "event": TX_UPDATED,
        "data": {"transaction": dict(tx_fields)},
        "environment": environment,
    }


def _signed_non_transaction_body(environment, secret):
    body = {
        "event": "nequi_token.updated",
        "data": {"transaction": {}},
        "environment": environment,
        "timestamp": 1784144472,
        "signature": {"properties": [], "checksum": ""},
    }
    body["signature"]["checksum"] = hashlib.sha256(
        f"{body['timestamp']}{secret}".encode()
    ).hexdigest()
    return body


@pytest.fixture
def background_tasks():
    return BackgroundTasks()


@pytest.mark.asyncio
async def test_classify_wt_reference_routes_tickets():
    body = {"data": {"transaction": {"reference": "WT-abc12345-1717200000"}}}
    assert await classify_transaction_updated(body) == WompiRoute.TICKETS


@pytest.mark.asyncio
async def test_classify_gateway_reference_routes_colombia():
    body = {"data": {"transaction": {"payment_link_id": "SD7wnV"}}}
    with patch(
        "app.services.wompi_webhook_router_service._gateway_reference_exists",
        AsyncMock(return_value=True),
    ):
        assert await classify_transaction_updated(body) == WompiRoute.COLOMBIA


@pytest.mark.asyncio
async def test_classify_billing_redirect_routes_colombia():
    body = {
        "data": {
            "transaction": {
                "redirect_url": "https://warocol.com/billing/confirmacion",
            }
        }
    }
    with patch(
        "app.services.wompi_webhook_router_service._gateway_reference_exists",
        AsyncMock(return_value=False),
    ):
        assert await classify_transaction_updated(body) == WompiRoute.COLOMBIA


@pytest.mark.asyncio
async def test_classify_unknown_when_no_signals():
    body = {"data": {"transaction": {"reference": "OTHER-123"}}}
    with patch(
        "app.services.wompi_webhook_router_service._gateway_reference_exists",
        AsyncMock(return_value=False),
    ), patch(
        "app.services.wompi_webhook_router_service._tenant_id_exists",
        AsyncMock(return_value=False),
    ):
        assert await classify_transaction_updated(body) == WompiRoute.UNKNOWN


@pytest.mark.asyncio
async def test_dispatch_wt_reference_forwards_tickets_not_colombia(background_tasks):
    body = _transaction_body(reference="WT-abc-123")
    forward = AsyncMock()
    colombia = AsyncMock()
    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=True,
    ), patch(
        "app.services.wompi_webhook_router_service.forward_to_tickets",
        forward,
    ), patch(
        "app.services.wompi_webhook_router_service.wompi_colombia_webhook_service.handle_transaction_updated",
        colombia,
    ):
        result = await dispatch_verified_event(body, background_tasks)

    assert result == {"status": "received"}
    forward.assert_awaited_once_with(body)
    colombia.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_gateway_reference_calls_colombia_not_tickets(background_tasks):
    body = _transaction_body(payment_link_id="SD7wnV")
    forward = AsyncMock()
    colombia = AsyncMock()
    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=True,
    ), patch(
        "app.services.wompi_webhook_router_service._gateway_reference_exists",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.wompi_webhook_router_service.forward_to_tickets",
        forward,
    ), patch(
        "app.services.wompi_webhook_router_service.wompi_colombia_webhook_service.handle_transaction_updated",
        colombia,
    ):
        result = await dispatch_verified_event(body, background_tasks)

    assert result == {"received": True}
    colombia.assert_awaited_once_with(
        body, background_tasks, provider_environment="prod"
    )
    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_billing_redirect_calls_colombia_not_tickets(background_tasks):
    body = _transaction_body(
        redirect_url="https://warocol.com/billing/confirmacion",
    )
    forward = AsyncMock()
    colombia = AsyncMock()
    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=True,
    ), patch(
        "app.services.wompi_webhook_router_service._gateway_reference_exists",
        AsyncMock(return_value=False),
    ), patch(
        "app.services.wompi_webhook_router_service.forward_to_tickets",
        forward,
    ), patch(
        "app.services.wompi_webhook_router_service.wompi_colombia_webhook_service.handle_transaction_updated",
        colombia,
    ):
        result = await dispatch_verified_event(body, background_tasks)

    assert result == {"received": True}
    colombia.assert_awaited_once_with(
        body, background_tasks, provider_environment="prod"
    )
    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_unknown_skips_downstream(background_tasks):
    body = _transaction_body(reference="OTHER-999")
    forward = AsyncMock()
    colombia = AsyncMock()
    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=True,
    ), patch(
        "app.services.wompi_webhook_router_service._gateway_reference_exists",
        AsyncMock(return_value=False),
    ), patch(
        "app.services.wompi_webhook_router_service._tenant_id_exists",
        AsyncMock(return_value=False),
    ), patch(
        "app.services.wompi_webhook_router_service.forward_to_tickets",
        forward,
    ), patch(
        "app.services.wompi_webhook_router_service.wompi_colombia_webhook_service.handle_transaction_updated",
        colombia,
    ):
        result = await dispatch_verified_event(body, background_tasks)

    assert result == {"received": True, "classification": "unknown"}
    forward.assert_not_awaited()
    colombia.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_invalid_signature_raises_401(background_tasks):
    body = _transaction_body(reference="WT-abc-123")
    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=False,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await dispatch_verified_event(body, background_tasks)

    assert exc_info.value.status_code == 401


def test_central_webhook_endpoint_rejects_invalid_signature():
    app = FastAPI()
    app.include_router(payments_webhook_router)
    body = _transaction_body(reference="WT-abc-123")

    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=False,
    ):
        client = TestClient(app)
        response = client.post("/payments/webhooks/wompi", json=body)

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()


def test_production_and_sandbox_endpoints_use_separate_signed_environments():
    app = FastAPI()
    app.include_router(payments_webhook_router)
    client = TestClient(app)
    prod_body = _signed_non_transaction_body("prod", "prod-secret")
    test_body = _signed_non_transaction_body("test", "test-secret")

    with patch(
        "app.services.wompi_service.settings.wompi_events_secret",
        "prod-secret",
    ), patch(
        "app.services.wompi_service.settings.wompi_sandbox_events_secret",
        "test-secret",
    ):
        assert client.post("/payments/webhooks/wompi", json=prod_body).status_code == 200
        assert client.post("/payments/webhooks/wompi", json=test_body).status_code == 401
        assert client.post(
            "/payments/webhooks/wompi/sandbox", json=test_body
        ).status_code == 200
        assert client.post(
            "/payments/webhooks/wompi/sandbox", json=prod_body
        ).status_code == 401


@pytest.mark.asyncio
async def test_sandbox_ticket_reference_is_not_forwarded(background_tasks):
    body = _transaction_body(environment="test", reference="WT-abc-123")
    forward = AsyncMock()
    with patch(
        "app.services.wompi_webhook_router_service.wompi_service.verify_event_signature",
        return_value=True,
    ), patch(
        "app.services.wompi_webhook_router_service.forward_to_tickets",
        forward,
    ):
        result = await dispatch_verified_event(
            body, background_tasks, expected_environment="test"
        )

    assert result == {"received": True, "classification": "unknown"}
    forward.assert_not_awaited()
