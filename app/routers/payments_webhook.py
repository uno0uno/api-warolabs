"""
Payment provider webhook ingress.

Public URLs:
  POST /payments/webhooks/wompi[+sandbox]   — Tickets forward; Colombia billing no-op (#798)
  POST /payments/webhooks/paddle[+sandbox]  — legacy until #944
  POST /payments/webhooks/lemon-squeezy[+sandbox]  — SaaS MoR (#942)

Legacy Colombia Wompi URL remains: POST /billing/webhook (also no-op for billing activate)
"""
import json

from fastapi import APIRouter, BackgroundTasks, Request

from app.services import lemon_squeezy_service, paddle_service, wompi_webhook_router_service

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
async def paddle_live_webhook(request: Request, background_tasks: BackgroundTasks):
    """Paddle Billing live webhook — signature verified with live secret (#795)."""
    raw = await request.body()
    paddle_service.verify_paddle_signature(
        raw_body=raw,
        signature_header=request.headers.get("Paddle-Signature"),
        environment="prod",
    )
    payload = json.loads(raw.decode("utf-8"))
    return await paddle_service.handle_verified_webhook(
        payload, environment="prod", background_tasks=background_tasks
    )


@router.post("/paddle/sandbox", status_code=200)
async def paddle_sandbox_webhook(request: Request, background_tasks: BackgroundTasks):
    """Paddle Billing sandbox webhook — signature verified with sandbox secret (#795)."""
    raw = await request.body()
    paddle_service.verify_paddle_signature(
        raw_body=raw,
        signature_header=request.headers.get("Paddle-Signature"),
        environment="test",
    )
    payload = json.loads(raw.decode("utf-8"))
    return await paddle_service.handle_verified_webhook(
        payload, environment="test", background_tasks=background_tasks
    )


@router.post("/lemon-squeezy", status_code=200)
async def lemon_squeezy_live_webhook(request: Request, background_tasks: BackgroundTasks):
    """Lemon Squeezy live webhook — X-Signature verified (#942)."""
    raw = await request.body()
    lemon_squeezy_service.verify_lemon_squeezy_signature(
        raw_body=raw,
        signature_header=request.headers.get("X-Signature"),
        environment="prod",
    )
    payload = json.loads(raw.decode("utf-8"))
    return await lemon_squeezy_service.handle_verified_webhook(
        payload, environment="prod", background_tasks=background_tasks
    )


@router.post("/lemon-squeezy/sandbox", status_code=200)
async def lemon_squeezy_sandbox_webhook(
    request: Request, background_tasks: BackgroundTasks
):
    """Lemon Squeezy sandbox webhook — isolated signing secret (#942)."""
    raw = await request.body()
    lemon_squeezy_service.verify_lemon_squeezy_signature(
        raw_body=raw,
        signature_header=request.headers.get("X-Signature"),
        environment="test",
    )
    payload = json.loads(raw.decode("utf-8"))
    return await lemon_squeezy_service.handle_verified_webhook(
        payload, environment="test", background_tasks=background_tasks
    )
