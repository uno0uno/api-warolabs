"""MercadoPago preapproval (subscription) for CO — COP 30k (issue #2700)."""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import httpx

from app.config import settings

MP_API = "https://api.mercadopago.com/preapproval"
PROVIDER = "mercadopago"


def _access_token(environment: str = "prod") -> Optional[str]:
    if environment == "test":
        return getattr(settings, "mercadopago_access_token_test", None) or getattr(settings, "mercadopago_access_token", None)
    return getattr(settings, "mercadopago_access_token", None) or getattr(settings, "mercadopago_access_token_test", None)


def _webhook_secret() -> Optional[str]:
    return getattr(settings, "mercadopago_webhook_secret", None)


async def create_preapproval(
    *,
    payer_email: str,
    back_url: str,
    notification_url: str,
    environment: str = "prod",
    reason: str = "WARO Pro - COP 30.000/mes",
    amount: float = 30000.0,
) -> dict:
    token = _access_token(environment)
    if not token:
        raise ValueError("MERCADOPAGO_ACCESS_TOKEN not configured")
    # Use preapproval_plan if exists (created for CO) — MP requires plan for Suscripciones
    # Dualidad prod/sandbox por tenant (igual que LS/Matias) — waro-colombia usa test en prod
    plan_id = getattr(settings, "mercadopago_preapproval_plan_id_test" if environment=="test" else "mercadopago_preapproval_plan_id", None) or getattr(settings, "mercadopago_preapproval_plan_id", None) or "1b289f4a52fc460b93582bc165b3d2c6"
    payload = {
        "preapproval_plan_id": plan_id,
        "payer_email": payer_email,
        "back_url": back_url,
        "notification_url": notification_url,
        "reason": reason,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            MP_API,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            import logging; logging.getLogger(__name__).error("MP preapproval error %s %s", resp.status_code, resp.text[:2000])
        resp.raise_for_status()
        data = resp.json()
        return {
            "preapproval_id": data.get("id"),
            "init_point": data.get("init_point") or data.get("init_point"),
            "raw": data,
        }


def verify_signature(*, raw_body: bytes, signature: Optional[str], request_id: Optional[str]) -> bool:
    secret = _webhook_secret()
    if not secret:
        return True  # dev: no secret configured
    if not signature:
        return False
    # MP x-signature: ts=...,v1=...
    try:
        parts = dict(p.split("=", 1) for p in signature.split(",") if "=" in p)
        ts = parts.get("ts", "")
        v1 = parts.get("v1", "")
        manifest = f"id:{request_id or ''};request-id:{request_id or ''};ts:{ts};"
        expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)
    except Exception:
        return False
