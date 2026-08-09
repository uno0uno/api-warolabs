"""
Payment provider webhook ingress.

Public URLs:
  POST /payments/webhooks/wompi[+sandbox]
  POST /payments/webhooks/paddle[+sandbox]

Legacy Colombia Wompi URL remains: POST /billing/webhook
"""
import json

from fastapi import APIRouter, BackgroundTasks, Request

from app.services import paddle_service, wompi_webhook_router_service

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


@router.post("/paddle", status_code=200)
async def paddle_live_webhook(request: Request):
    """Paddle Billing live webhook — signature verified with live secret (#795)."""
    raw = await request.body()
    paddle_service.verify_paddle_signature(
        raw_body=raw,
        signature_header=request.headers.get("Paddle-Signature"),
        environment="prod",
    )
    payload = json.loads(raw.decode("utf-8"))
    return await paddle_service.handle_verified_webhook(payload, environment="prod")


@router.post("/paddle/sandbox", status_code=200)
async def paddle_sandbox_webhook(request: Request):
    """Paddle Billing sandbox webhook — signature verified with sandbox secret (#795)."""
    raw = await request.body()
    paddle_service.verify_paddle_signature(
        raw_body=raw,
        signature_header=request.headers.get("Paddle-Signature"),
        environment="test",
    )
    payload = json.loads(raw.decode("utf-8"))
    return await paddle_service.handle_verified_webhook(payload, environment="test")
