"""Wompi central ingress classification (#353)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.services.wompi_webhook_router_service import (
    WompiRoute,
    classify_transaction_updated,
)


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
