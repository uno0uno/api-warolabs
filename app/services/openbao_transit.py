"""OpenBao Transit client for restaurant Wompi key envelope encryption (#862)."""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.core.exceptions import APIError

logger = logging.getLogger(__name__)

_TRANSIT_KEY = "wompi-merchant"
_token: Optional[str] = None
_token_exp = 0.0


def _read_secret(value: Optional[str], file_path: Optional[str]) -> Optional[str]:
    if value and value.strip():
        return value.strip()
    if file_path:
        path = Path(file_path)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return None


def _addr() -> str:
    return (settings.openbao_addr or "http://openbao:8200").rstrip("/")


def _credentials() -> tuple[str, str]:
    role_id = _read_secret(settings.openbao_role_id, settings.openbao_role_id_file)
    secret_id = _read_secret(settings.openbao_secret_id, settings.openbao_secret_id_file)
    if not role_id or not secret_id:
        raise APIError(
            "OpenBao AppRole no está configurado",
            status_code=503,
            details={"code": "OPENBAO_NOT_CONFIGURED"},
        )
    return role_id, secret_id


async def _login() -> str:
    global _token, _token_exp
    now = time.time()
    if _token and now < _token_exp:
        return _token
    role_id, secret_id = _credentials()
    url = f"{_addr()}/v1/auth/approle/login"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json={"role_id": role_id, "secret_id": secret_id}
            )
    except httpx.RequestError as exc:
        logger.error("OpenBao login connection error")
        raise APIError(
            "OpenBao no está disponible",
            status_code=503,
            details={"code": "OPENBAO_UNREACHABLE"},
        ) from exc
    if response.status_code >= 400:
        logger.error("OpenBao login failed status=%s", response.status_code)
        raise APIError(
            "OpenBao rechazó AppRole (¿sellado o sin política?)",
            status_code=503,
            details={"code": "OPENBAO_AUTH_FAILED"},
        )
    auth = response.json().get("auth") or {}
    token = auth.get("client_token")
    if not token:
        raise APIError(
            "OpenBao no devolvió token",
            status_code=503,
            details={"code": "OPENBAO_AUTH_FAILED"},
        )
    ttl = int(auth.get("lease_duration") or 300)
    _token = token
    _token_exp = now + max(30, ttl - 30)
    return token


async def encrypt_plaintext(plaintext: str) -> str:
    token = await _login()
    payload = {
        "plaintext": base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    }
    url = f"{_addr()}/v1/transit/encrypt/{_TRANSIT_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json=payload, headers={"X-Vault-Token": token}
            )
    except httpx.RequestError as exc:
        logger.error("OpenBao encrypt connection error")
        raise APIError(
            "OpenBao no está disponible",
            status_code=503,
            details={"code": "OPENBAO_UNREACHABLE"},
        ) from exc
    if response.status_code >= 400:
        logger.error("OpenBao encrypt failed status=%s", response.status_code)
        raise APIError(
            "No se pudo cifrar con OpenBao Transit",
            status_code=503,
            details={"code": "OPENBAO_ENCRYPT_FAILED"},
        )
    ciphertext = (response.json().get("data") or {}).get("ciphertext")
    if not ciphertext:
        raise APIError(
            "OpenBao no devolvió ciphertext",
            status_code=503,
            details={"code": "OPENBAO_ENCRYPT_FAILED"},
        )
    return ciphertext


async def decrypt_ciphertext(ciphertext: str) -> str:
    token = await _login()
    url = f"{_addr()}/v1/transit/decrypt/{_TRANSIT_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"ciphertext": ciphertext},
                headers={"X-Vault-Token": token},
            )
    except httpx.RequestError as exc:
        logger.error("OpenBao decrypt connection error")
        raise APIError(
            "OpenBao no está disponible",
            status_code=503,
            details={"code": "OPENBAO_UNREACHABLE"},
        ) from exc
    if response.status_code >= 400:
        logger.error("OpenBao decrypt failed status=%s", response.status_code)
        raise APIError(
            "No se pudo descifrar con OpenBao Transit",
            status_code=503,
            details={"code": "OPENBAO_DECRYPT_FAILED"},
        )
    b64 = (response.json().get("data") or {}).get("plaintext")
    if not b64:
        raise APIError(
            "OpenBao no devolvió plaintext",
            status_code=503,
            details={"code": "OPENBAO_DECRYPT_FAILED"},
        )
    return base64.b64decode(b64).decode("utf-8")
