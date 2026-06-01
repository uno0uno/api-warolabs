"""Wompi central ingress classification and dispatch (#353, #355)."""
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


def _transaction_body(**tx_fields):
    return {
        "event": TX_UPDATED,
        "data": {"transaction": dict(tx_fields)},
    }


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
    colombia.assert_awaited_once_with(body, background_tasks)
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
    colombia.assert_awaited_once_with(body, background_tasks)
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
