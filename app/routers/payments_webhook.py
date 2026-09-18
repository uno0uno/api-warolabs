"""
Payment provider webhook ingress.

Public URLs:
  POST /payments/webhooks/wompi[+sandbox]   — Tickets forward; Colombia billing no-op (#798)
  POST /payments/webhooks/lemon-squeezy[+sandbox]  — SaaS MoR (#942 / #944)

Legacy Colombia Wompi URL remains: POST /billing/webhook (also no-op for billing activate)
"""
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request

from app.database import get_db_connection
from app.services import lemon_squeezy_service, mercadopago_subscription_service, wompi_webhook_router_service

logger = logging.getLogger(__name__)

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
    # Idempotent activation: whoever arrives first (webhook or fallback confirm) wins (#1020)
    try:
        preapproval_id = (
            (payload.get("data") or {}).get("id")
            or payload.get("data_id")
            or payload.get("id")
            or payload.get("preapproval_id")
        )
        if preapproval_id:
            preapproval_id = str(preapproval_id).strip()
        if preapproval_id:
            # Resolve MP status via API (test→prod, same as billing.py:440 fallback)
            mp_data = None
            mp_status = ""
            mp_env_used = None
            for env in ("test", "prod"):
                try:
                    d = await mercadopago_subscription_service.get_preapproval_status(
                        preapproval_id=preapproval_id, environment=env
                    )
                    if d:
                        mp_data = d
                        mp_status = (d.get("status") or "").lower()
                        mp_env_used = env
                        break
                except Exception as e:
                    logger.warning("MP webhook get_preapproval %s env=%s err=%s", preapproval_id, env, e)
            if mp_data and mp_status in ("authorized", "active"):
                payer_obj = mp_data.get("payer") if isinstance(mp_data.get("payer"), dict) else {}
                payer_email = (mp_data.get("payer_email") or payer_obj.get("email") or "").strip().lower()
                tenant_id = None
                if payer_email:
                    async with get_db_connection(use_transaction=False) as conn:
                        row = await conn.fetchrow(
                            "SELECT id FROM tenants WHERE lower(email)=$1 LIMIT 1", payer_email
                        )
                        if row:
                            tenant_id = row["id"]
                        else:
                            row2 = await conn.fetchrow(
                                """
                                SELECT tenant_id FROM onboarding_payment_attempts
                                WHERE lower(payer_email)=$1 ORDER BY created_at DESC LIMIT 1
                                """,
                                payer_email,
                            )
                            if row2:
                                tenant_id = row2["tenant_id"]
                if tenant_id:
                    async with get_db_connection() as conn:
                        await conn.execute(
                            "UPDATE tenant_subscriptions SET status='active', current_period_end = NOW() + INTERVAL '30 days', updated_at=NOW() WHERE tenant_id=$1 AND status != 'active'",
                            tenant_id,
                        )
                        # atomic dedupe: single-statement insert-if-not-exists (#1020 race)
                        await conn.execute(
                            """
                            INSERT INTO billing_events (tenant_id, event_type, metadata)
                            SELECT $1, 'payment_approved', $2::jsonb
                            WHERE NOT EXISTS (
                                SELECT 1 FROM billing_events
                                WHERE tenant_id=$1 AND event_type='payment_approved' AND metadata->>'preapproval_id'=$3
                            )
                            """,
                            tenant_id,
                            json.dumps({"provider": "mercadopago", "preapproval_id": preapproval_id, "mp_status": mp_status, "env": mp_env_used, "source": "webhook"}),
                            preapproval_id,
                        )
                    logger.info("MP webhook activated tenant=%s preapproval=%s status=%s", tenant_id, preapproval_id, mp_status)
                    return {"received": True, "provider": "mercadopago", "activated": True, "preapproval_id": preapproval_id}
                else:
                    logger.warning("MP webhook no tenant for payer_email=%s preapproval=%s", payer_email, preapproval_id)
    except Exception as e:
        logger.exception("MP webhook handler error preapproval=%s err=%s", payload.get("data", {}).get("id") if isinstance(payload.get("data"), dict) else payload.get("id"), e)
        # still ack 200 to avoid MP retry storm; activation will be retried via confirm fallback
    return {"received": True, "provider": "mercadopago"}
