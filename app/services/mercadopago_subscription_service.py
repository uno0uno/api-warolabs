"""MercadoPago preapproval (subscription) for CO — COP 30k (issue #2700)."""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import httpx

from app.config import settings

MP_API = "https://api.mercadopago.com/preapproval"
PROVIDER = "mercadopago"


def _access_token() -> Optional[str]:
    return getattr(settings, "mercadopago_access_token", None) or getattr(settings, "mp_access_token", None)


def _webhook_secret() -> Optional[str]:
    return getattr(settings, "mercadopago_webhook_secret", None)


async def create_preapproval(
    *,
    payer_email: str,
    back_url: str,
    notification_url: str,
    reason: str = "WARO Pro - COP 30.000/mes",
    amount: float = 30000.0,
) -> dict:
    token = _access_token()
    if not token:
        raise ValueError("MERCADOPAGO_ACCESS_TOKEN not configured")
    payload = {
        "reason": reason,
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": amount,
            "currency_id": "COP",
        },
        "back_url": back_url,
        "payer_email": payer_email,
        "notification_url": notification_url,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            MP_API,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
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
