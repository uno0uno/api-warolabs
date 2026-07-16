"""
Central Wompi webhook ingress (api-warolabs #353).

Public URL: POST /payments/webhooks/wompi
Legacy Colombia URL remains: POST /billing/webhook
"""
from fastapi import APIRouter, BackgroundTasks, Request

from app.services import wompi_webhook_router_service

router = APIRouter(prefix="/payments/webhooks", tags=["Payments Webhooks"])


@router.post("/wompi", status_code=200)
async def wompi_central_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Central Wompi merchant webhook — verify once, classify, dispatch.

    See docs/payments/wompi-webhook-routing.md.
    """
    body = await request.json()
    return await wompi_webhook_router_service.dispatch_verified_event(
        body, background_tasks, expected_environment="prod"
    )


@router.post("/wompi/sandbox", status_code=200)
async def wompi_sandbox_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Sandbox Wompi webhook; isolated from the production events secret."""
    body = await request.json()
    return await wompi_webhook_router_service.dispatch_verified_event(
        body, background_tasks, expected_environment="test"
    )
