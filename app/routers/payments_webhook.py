"""
Payment provider webhook ingress.

Public URLs:
  POST /payments/webhooks/wompi[+sandbox]   — Tickets forward; Colombia billing no-op (#798)
  POST /payments/webhooks/lemon-squeezy[+sandbox]  — SaaS MoR (#942 / #944)

Legacy Colombia Wompi URL remains: POST /billing/webhook (also no-op for billing activate)
"""
import json

from fastapi import APIRouter, BackgroundTasks, Request

from app.services import lemon_squeezy_service, mercadopago_subscription_service, wompi_webhook_router_service

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


@router.post("/mercadopago", status_code=200)
async def mercadopago_webhook(request: Request, background_tasks: BackgroundTasks):
    """MercadoPago webhook for CO preapproval payments (#2700)."""
    raw = await request.body()
    # MP sends x-signature + x-request-id
    # Dual TEST/LIVE igual que LS — prueba ambos secrets (waro-colombia test en prod)
    ok = any(
        mercadopago_subscription_service.verify_signature(
            raw_body=raw,
            signature=request.headers.get("x-signature") or request.headers.get("X-Signature"),
            request_id=request.headers.get("x-request-id") or request.headers.get("X-Request-Id"),
            environment=env,
        )
        for env in ("test", "prod")
    )
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Invalid MP signature")
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    # Expect payment type; fallback to raw
    return {"received": True, "provider": "mercadopago"}
