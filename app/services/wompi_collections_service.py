"""Restaurant Wompi diner collections (#862). Uses tenant keys; OpenBao envelope."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import Request

from app.core.exceptions import NotFoundError, ValidationError
from app.core.middleware import require_valid_session
from app.core.timezones import local_date_for_tenant, resolve_tenant_timezone
from app.database import get_db_connection
from app.services import openbao_transit
from asyncpg import UniqueViolationError
from app.services.cierre_service import (
    _get_tenant_tax_config,
    _post_order_cogs_gl_entry,
    _post_order_gl_entry,
)
from app.services.account_role_service import resolve_group_parent_account
from app.services.customer_relationship_service import is_tenant_customer
from app.services.customers_service import ANONYMOUS_PHONE, GENERIC_CUSTOMER_EMAIL
from app.services.wompi_service import WOMPI_PRODUCTION_URL, WOMPI_SANDBOX_URL

logger = logging.getLogger(__name__)

WOMPI_METHOD_NAME = "Wompi"
DIGITAL_SLUG = "digital"
NOTIFY_TYPE = "order_payment_approved"
_THANK_YOU_HOSTS = {"warocol.com", "www.warocol.com", "localhost", "127.0.0.1"}


def _safe_thank_you_url(redirect_url: Optional[str], session_id: UUID) -> str:
    default = f"https://warocol.com/cobro/{session_id}/gracias"
    if not redirect_url:
        return default
    resolved = redirect_url.replace("{sessionId}", str(session_id)).strip()
    parsed = urlparse(resolved)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if parsed.scheme not in ("https", "http"):
        return default
    if host not in _THANK_YOU_HOSTS:
        return default
    if path != f"/cobro/{session_id}/gracias":
        return default
    return resolved


def _jsonable(value: Any) -> Any:
    """Recursively coerce Decimal/datetime/UUID to JSON-safe primitives for JSONB columns."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _wompi_payment_link_redirect(redirect_url: Optional[str], session_id: UUID) -> str:
    """Pass through staff thank-you origin (localhost in local, warocol.com in prod)."""
    return _safe_thank_you_url(redirect_url, session_id)


def _wompi_base_url(environment: str) -> str:
    if environment == "prod":
        return WOMPI_PRODUCTION_URL
    return WOMPI_SANDBOX_URL


def _environment_from_private_key(private_key: str) -> str:
    key = private_key.strip().lower()
    if key.startswith("prv_prod_"):
        return "prod"
    return "test"


def fingerprint_public_key(public_key: str) -> str:
    digest = hashlib.sha256(public_key.strip().encode("utf-8")).hexdigest()[:12]
    tail = public_key.strip()[-4:]
    return f"{digest}:{tail}"


def restaurant_headers(private_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {private_key}",
        "Content-Type": "application/json",
    }


def verify_event_signature_with_secret(
    event_data: Dict[str, Any], events_secret: str, expected_environment: str
) -> bool:
    if event_data.get("environment") != expected_environment:
        return False
    signature_data = event_data.get("signature") or {}
    properties = signature_data.get("properties") or []
    checksum = signature_data.get("checksum") or ""
    if not checksum or not events_secret:
        return False
    transaction = (event_data.get("data") or {}).get("transaction") or {}
    values = []
    for prop in properties:
        key = prop.replace("transaction.", "") if str(prop).startswith("transaction.") else prop
        values.append(str(transaction.get(key, "")))
    values.append(str(event_data.get("timestamp", "")))
    values.append(events_secret)
    computed = hashlib.sha256("".join(values).encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, checksum)


def merchant_public_keys_from_wompi_body(payload: Any) -> list[str]:
    """Wompi GET /merchants may return data as an object or a list."""
    found: list[str] = []
    if isinstance(payload, dict):
        key = payload.get("public_key")
        if isinstance(key, str) and key.strip():
            found.append(key.strip())
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found.extend(merchant_public_keys_from_wompi_body(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(merchant_public_keys_from_wompi_body(item))
    return found


def payment_link_id_from_wompi_body(payload: Any) -> Optional[str]:
    """Wompi POST /payment_links may return data as an object or a list."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            link_id = data.get("id")
            if isinstance(link_id, str) and link_id.strip():
                return link_id.strip()
        if isinstance(data, list):
            for item in data:
                found = payment_link_id_from_wompi_body({"data": item})
                if found:
                    return found
        if isinstance(data, str) and data.strip():
            return data.strip()
    elif isinstance(payload, list):
        for item in payload:
            found = payment_link_id_from_wompi_body(item)
            if found:
                return found
    return None


def wompi_resource_data(payload: Any) -> Any:
    """Unwrap Wompi `data` whether it is an object, a list, or missing."""
    if isinstance(payload, dict):
        return payload.get("data")
    return payload


async def validate_merchant_keys(public_key: str, private_key: str, environment: str) -> None:
    url = f"{_wompi_base_url(environment)}/merchants"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=restaurant_headers(private_key))
    except httpx.RequestError as exc:
        logger.error("Wompi merchants lookup connection error")
        raise ValidationError("No se pudo validar el comercio en Wompi") from exc
    if response.status_code >= 400:
        raise ValidationError("Las llaves Wompi del restaurante no son válidas")
    remote_pubs = merchant_public_keys_from_wompi_body(response.json())
    submitted = public_key.strip()
    if remote_pubs and submitted not in remote_pubs:
        raise ValidationError("La llave pública no corresponde a este comercio Wompi")


def next_puc_child_code(parent_code: str, taken: set[str]) -> str:
    base = (parent_code or "").strip()
    if not base:
        raise ValidationError("Digitales no tiene cuenta padre")
    preferred = base + "05"
    if preferred not in taken:
        return preferred
    for n in range(1, 100):
        candidate = f"{base}{n:02d}"
        if candidate not in taken:
            return candidate
    raise ValidationError("No hay código PUC hijo disponible bajo Digitales")


async def _generic_customer_id(conn, tenant_id: UUID) -> UUID:
    row = await conn.fetchrow(
        """
        SELECT p.id
        FROM profile p
        JOIN tenant_customers tc ON tc.profile_id = p.id AND tc.tenant_id = $1 AND tc.is_active = true
        WHERE p.phone_number = $2
        ORDER BY
          CASE
            WHEN lower(trim(coalesce(p.email, ''))) = $3 THEN 0
            WHEN lower(trim(coalesce(p.name, ''))) IN ('genérico', 'generico') THEN 1
            ELSE 2
          END,
          p.created_at ASC NULLS LAST
        LIMIT 1
        """,
        tenant_id,
        ANONYMOUS_PHONE,
        GENERIC_CUSTOMER_EMAIL,
    )
    if not row:
        raise ValidationError("No hay cliente Genérico para este negocio")
    return row["id"]


async def resolve_collection_customer(
    conn, tenant_id: UUID, selected_customer_id: Optional[UUID]
) -> UUID:
    if selected_customer_id is None:
        return await _generic_customer_id(conn, tenant_id)
    if not await is_tenant_customer(conn, selected_customer_id, tenant_id):
        raise ValidationError("El cliente no pertenece a este negocio")
    return selected_customer_id


async def activate_merchant(
    request: Request,
    public_key: str,
    private_key: str,
    events_secret: str,
    integrity_secret: Optional[str] = None,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    environment = _environment_from_private_key(private_key)
    await validate_merchant_keys(public_key, private_key, environment)
    private_ct = await openbao_transit.encrypt_plaintext(private_key)
    events_ct = await openbao_transit.encrypt_plaintext(events_secret)
    integrity_ct = (
        await openbao_transit.encrypt_plaintext(integrity_secret)
        if integrity_secret
        else None
    )
    fp = fingerprint_public_key(public_key)

    async with get_db_connection(use_transaction=True) as conn:
        group = await conn.fetchrow(
            """
            SELECT id, tenant_id, gl_account_id, gl_account_code
            FROM payment_method_groups
            WHERE slug = $1 AND (tenant_id IS NULL OR tenant_id = $2)
            ORDER BY tenant_id NULLS LAST
            LIMIT 1
            """,
            DIGITAL_SLUG,
            tenant_id,
        )
        if group is None:
            raise ValidationError("No existe el grupo Digitales")
        parent = await resolve_group_parent_account(
            conn,
            tenant_id,
            slug=DIGITAL_SLUG,
            gl_account_id=group["gl_account_id"],
            gl_account_code=group["gl_account_code"],
            group_tenant_id=group["tenant_id"],
        )
        if parent is None:
            raise ValidationError("Digitales no tiene cuenta padre")
        taken_rows = await conn.fetch(
            "SELECT code FROM tenant_accounts WHERE tenant_id = $1 AND code LIKE $2",
            tenant_id,
            parent.code + "%",
        )
        taken = {row["code"] for row in taken_rows}
        child_code = next_puc_child_code(parent.code, taken)
        level = 6 if len(child_code) >= 6 else 4
        account = await conn.fetchrow(
            """
            INSERT INTO tenant_accounts
                (tenant_id, code, name, account_class, account_type, normal_balance,
                 level, parent_id, is_detail, is_system, is_active)
            VALUES ($1, $2, $3, $4, 'asset', 'debit', $5, $6, true, false, true)
            RETURNING id, code
            """,
            tenant_id,
            child_code,
            WOMPI_METHOD_NAME,
            (parent.code[:1] if parent.code else "1"),
            level,
            parent.id,
        )
        existing_method = await conn.fetchrow(
            """
            SELECT id FROM payment_methods
            WHERE tenant_id = $1 AND group_id = $2 AND name = $3
            """,
            tenant_id,
            group["id"],
            WOMPI_METHOD_NAME,
        )
        if existing_method:
            method_id = existing_method["id"]
            await conn.execute(
                """
                UPDATE payment_methods
                SET gl_account_code = $2, gl_account_id = $3, is_active = true
                WHERE id = $1
                """,
                method_id,
                account["code"],
                account["id"],
            )
        else:
            method = await conn.fetchrow(
                """
                INSERT INTO payment_methods
                    (tenant_id, group_id, name, sort_order, gl_account_code, gl_account_id)
                VALUES ($1, $2, $3, 0, $4, $5)
                RETURNING id
                """,
                tenant_id,
                group["id"],
                WOMPI_METHOD_NAME,
                account["code"],
                account["id"],
            )
            method_id = method["id"]
        await conn.execute(
            """
            INSERT INTO tenant_payment_providers (
                tenant_id, public_key, fingerprint, private_key_ciphertext,
                events_secret_ciphertext, integrity_secret_ciphertext,
                payment_method_id, environment, is_active, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                public_key = EXCLUDED.public_key,
                fingerprint = EXCLUDED.fingerprint,
                private_key_ciphertext = EXCLUDED.private_key_ciphertext,
                events_secret_ciphertext = EXCLUDED.events_secret_ciphertext,
                integrity_secret_ciphertext = EXCLUDED.integrity_secret_ciphertext,
                payment_method_id = EXCLUDED.payment_method_id,
                environment = EXCLUDED.environment,
                is_active = true,
                updated_at = NOW()
            """,
            tenant_id,
            public_key.strip(),
            fp,
            private_ct,
            events_ct,
            integrity_ct,
            method_id,
            environment,
        )
    return {
        "success": True,
        "data": {
            "fingerprint": fp,
            "environment": environment,
            "paymentMethodId": str(method_id),
            "glAccountCode": account["code"],
        },
    }


async def merchant_status(request: Request) -> dict:
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT fingerprint, environment, is_active, payment_method_id
            FROM tenant_payment_providers
            WHERE tenant_id = $1
            """,
            session.tenant_id,
        )
    if not row:
        return {"success": True, "data": None}
    return {
        "success": True,
        "data": {
            "fingerprint": row["fingerprint"],
            "environment": row["environment"],
            "isActive": row["is_active"],
            "paymentMethodId": str(row["payment_method_id"]) if row["payment_method_id"] else None,
        },
    }


async def _load_merchant(conn, tenant_id: UUID) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM tenant_payment_providers WHERE tenant_id = $1 AND is_active = true",
        tenant_id,
    )
    if not row:
        raise ValidationError("Pasarela Wompi no está activa")
    return row


async def public_collection_session(session_id: UUID) -> dict:
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT checkout_url, status, provider, provider_payment_method_type,
                   customer_email, currency, environment, provider_payload
            FROM tenant_collection_sessions
            WHERE id = $1
            """,
            session_id,
        )
    if not row:
        raise NotFoundError("Sesión de cobro no encontrada")
    return {
        "success": True,
        "data": {
            "checkoutUrl": row["checkout_url"],
            "status": row["status"],
            "provider": row["provider"],
            "providerPaymentMethodType": row["provider_payment_method_type"],
            "customerEmail": row["customer_email"],
            "currency": row["currency"],
            "environment": row["environment"],
            "providerPayload": row["provider_payload"],
        },
    }


def _session_reuse_payload(row: Any) -> dict:
    return {
        "success": True,
        "data": {
            "id": str(row["id"]),
            "status": row["status"],
            "customerId": str(row["customer_id"]) if row["customer_id"] else None,
        },
    }


async def _pending_session_for_order(conn, tenant_id: UUID, order_id: UUID):
    return await conn.fetchrow(
        """
        SELECT id, status, customer_id
        FROM tenant_collection_sessions
        WHERE tenant_id = $1 AND order_id = $2 AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        tenant_id,
        order_id,
    )


async def _create_or_reuse_session_row(
    conn,
    *,
    tenant_id: UUID,
    order_id: UUID,
    amount: Decimal,
    customer_id: UUID,
    link_email: Optional[str],
    redirect_url: Optional[str],
) -> dict:
    pending = await _pending_session_for_order(conn, tenant_id, order_id)
    if pending:
        return _session_reuse_payload(pending)
    try:
        return await _create_session_row(
            conn,
            tenant_id=tenant_id,
            order_id=order_id,
            amount=amount,
            customer_id=customer_id,
            link_email=link_email,
            redirect_url=redirect_url,
        )
    except UniqueViolationError:
        pending = await _pending_session_for_order(conn, tenant_id, order_id)
        if pending:
            return _session_reuse_payload(pending)
        raise ValidationError("Ya existe un cobro Wompi para esta orden")


async def staff_session_for_order(request: Request, order_id: UUID) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection() as conn:
        order = await conn.fetchval(
            "SELECT 1 FROM orders WHERE id = $1 AND tenant_id = $2",
            order_id,
            tenant_id,
        )
        if not order:
            raise NotFoundError("Orden no encontrada")
        row = await conn.fetchrow(
            """
            SELECT id, status
            FROM tenant_collection_sessions
            WHERE tenant_id = $1 AND order_id = $2
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, created_at DESC
            LIMIT 1
            """,
            tenant_id,
            order_id,
        )
        if not row:
            raise NotFoundError("Sesión de cobro no encontrada")
        return {
            "success": True,
            "data": {
                "id": str(row["id"]),
                "status": row["status"],
            },
        }


async def _post_approved_collection_gl(
    conn,
    *,
    tenant_id: UUID,
    order_id: UUID,
    payment_method: str,
    payment_method_id: Optional[UUID],
) -> None:
    order = await conn.fetchrow(
        """
        SELECT id, order_number, total_amount,
               COALESCE(tip_amount, 0) AS tip_amount,
               COALESCE(tip_tax_amount, 0) AS tip_tax_amount,
               order_date
        FROM orders
        WHERE id = $1 AND tenant_id = $2
        """,
        order_id,
        tenant_id,
    )
    if not order:
        return
    timezone_name = await resolve_tenant_timezone(conn, tenant_id)
    tax_config = await _get_tenant_tax_config(conn, tenant_id)
    gl_date = local_date_for_tenant(order["order_date"], timezone_name)
    await _post_order_gl_entry(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=gl_date,
        total_amount=Decimal(str(order["total_amount"])),
        payment_method=payment_method,
        payment_method_id=payment_method_id,
        tax_config=tax_config,
        order_number=int(order["order_number"]),
        tip_amount=Decimal(str(order["tip_amount"] or 0)),
        tip_tax_amount=Decimal(str(order["tip_tax_amount"] or 0)),
    )
    await _post_order_cogs_gl_entry(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=gl_date,
        order_number=int(order["order_number"]),
    )


async def _create_session_row(
    conn,
    *,
    tenant_id: UUID,
    order_id: UUID,
    amount: Decimal,
    customer_id: UUID,
    link_email: Optional[str],
    redirect_url: Optional[str],
) -> dict:
    merchant = await _load_merchant(conn, tenant_id)
    private_key = await openbao_transit.decrypt_ciphertext(
        merchant["private_key_ciphertext"]
    )
    session_id = uuid4()
    amount_cents = int((amount * 100).quantize(Decimal("1")))
    expiration = datetime.now(timezone.utc) + timedelta(hours=2)
    thank_you_url = _wompi_payment_link_redirect(redirect_url, session_id)
    payload = {
        "name": f"WARO cobro {order_id}",
        "description": "Cobro al comensal (restaurante)",
        "single_use": True,
        "collect_shipping": False,
        "currency": "COP",
        "amount_in_cents": amount_cents,
        "expires_at": expiration.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "redirect_url": thank_you_url,
        "reference": str(session_id),
    }
    if link_email:
        payload["customer_data"] = {"email": link_email}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{_wompi_base_url(merchant['environment'])}/payment_links",
                headers=restaurant_headers(private_key),
                json=payload,
            )
    except httpx.RequestError as exc:
        logger.error("Wompi payment_links connection error")
        raise ValidationError("Wompi no respondió al crear el link") from exc
    if response.status_code >= 400:
        logger.error(
            "Wompi payment_links rejected status=%s body=%s",
            response.status_code,
            (response.text or "")[:400],
        )
        raise ValidationError("Wompi rechazó la creación del link de cobro")
    try:
        body = response.json()
    except ValueError as exc:
        logger.error("Wompi payment_links returned non-JSON")
        raise ValidationError("Wompi no devolvió link de cobro") from exc
    link_id = payment_link_id_from_wompi_body(body)
    if not link_id:
        raise ValidationError("Wompi no devolvió link de cobro")
    checkout_url = f"https://checkout.wompi.co/l/{link_id}"
    await conn.execute(
        """
        INSERT INTO tenant_collection_sessions (
            id, tenant_id, order_id, amount, customer_id, link_email,
            provider_link_id, checkout_url, status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
        """,
        session_id,
        tenant_id,
        order_id,
        amount,
        customer_id,
        link_email,
        str(link_id),
        checkout_url,
    )
    return {
        "success": True,
        "data": {
            "id": str(session_id),
            "checkoutUrl": checkout_url,
            "status": "pending",
            "customerId": str(customer_id),
        },
    }


async def create_collection_session(
    request: Request,
    order_id: UUID,
    amount: Decimal,
    selected_customer_id: Optional[UUID] = None,
    link_email: Optional[str] = None,
    redirect_url: Optional[str] = None,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if amount <= 0:
        raise ValidationError("El monto debe ser positivo")
    async with get_db_connection(use_transaction=True) as conn:
        order = await conn.fetchrow(
            """
            SELECT id, customer_id, total_amount
            FROM orders
            WHERE id = $1 AND tenant_id = $2
            FOR UPDATE
            """,
            order_id,
            tenant_id,
        )
        if not order:
            raise NotFoundError("Orden no encontrada")
        customer_id = await resolve_collection_customer(
            conn, tenant_id, selected_customer_id or order["customer_id"]
        )
        if order["customer_id"] is None:
            await conn.execute(
                "UPDATE orders SET customer_id = $2 WHERE id = $1",
                order_id,
                customer_id,
            )
        return await _create_or_reuse_session_row(
            conn,
            tenant_id=tenant_id,
            order_id=order_id,
            amount=amount,
            customer_id=customer_id,
            link_email=link_email,
            redirect_url=redirect_url,
        )


async def regenerate_collection_session(
    request: Request,
    order_id: UUID,
    amount: Decimal,
    selected_customer_id: Optional[UUID] = None,
    link_email: Optional[str] = None,
    redirect_url: Optional[str] = None,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if amount <= 0:
        raise ValidationError("El monto debe ser positivo")
    async with get_db_connection(use_transaction=True) as conn:
        order = await conn.fetchrow(
            """
            SELECT id, customer_id, total_amount
            FROM orders
            WHERE id = $1 AND tenant_id = $2
            FOR UPDATE
            """,
            order_id,
            tenant_id,
        )
        if not order:
            raise NotFoundError("Orden no encontrada")
        approved = await conn.fetchval(
            """
            SELECT 1
            FROM tenant_collection_sessions
            WHERE tenant_id = $1 AND order_id = $2 AND status = 'approved'
            LIMIT 1
            """,
            tenant_id,
            order_id,
        )
        if approved:
            raise ValidationError("Este cobro ya fue aprobado")
        pending = await _pending_session_for_order(conn, tenant_id, order_id)
        if pending:
            await conn.execute(
                """
                UPDATE tenant_collection_sessions
                SET status = 'expired', updated_at = NOW()
                WHERE id = $1 AND status = 'pending'
                """,
                pending["id"],
            )
        customer_id = await resolve_collection_customer(
            conn, tenant_id, selected_customer_id or order["customer_id"]
        )
        return await _create_session_row(
            conn,
            tenant_id=tenant_id,
            order_id=order_id,
            amount=amount,
            customer_id=customer_id,
            link_email=link_email,
            redirect_url=redirect_url,
        )


async def create_online_collection_session(
    *,
    order_id: UUID,
    cart_id: UUID,
    amount: Decimal,
    link_email: Optional[str] = None,
    redirect_url: Optional[str] = None,
) -> dict:
    async with get_db_connection(use_transaction=True) as conn:
        order = await conn.fetchrow(
            """
            SELECT o.id, o.tenant_id, o.customer_id, o.online_cart_id,
                   o.total_amount, COALESCE(o.tip_amount, 0) AS tip_amount
            FROM orders o
            WHERE o.id = $1
            FOR UPDATE
            """,
            order_id,
        )
        if not order or order["online_cart_id"] != cart_id:
            raise NotFoundError("Orden no encontrada")
        paid = await conn.fetchval(
            """
            SELECT 1
            FROM order_payments
            WHERE order_id = $1 AND voided_at IS NULL
            LIMIT 1
            """,
            order_id,
        )
        if paid:
            raise ValidationError("La orden ya tiene un pago")
        due = Decimal(str(order["total_amount"])) + Decimal(str(order["tip_amount"] or 0))
        if due <= 0:
            raise ValidationError("El monto debe ser positivo")
        customer_id = await resolve_collection_customer(
            conn, order["tenant_id"], order["customer_id"]
        )
        return await _create_or_reuse_session_row(
            conn,
            tenant_id=order["tenant_id"],
            order_id=order_id,
            amount=due,
            customer_id=customer_id,
            link_email=link_email,
            redirect_url=redirect_url,
        )


async def apply_approved_payment(
    conn,
    *,
    tenant_id: UUID,
    session_row: Any,
    provider_tx_id: str,
    amount: Optional[Decimal] = None,
    provider_payload: Optional[Dict[str, Any]] = None,
) -> dict:
    if session_row["status"] == "approved" and session_row["order_payment_id"]:
        return {
            "applied": False,
            "idempotent": True,
            "orderPaymentId": str(session_row["order_payment_id"]),
        }
    already_paid = await conn.fetchval(
        """
        SELECT 1
        FROM order_payments
        WHERE order_id = $1 AND voided_at IS NULL
        LIMIT 1
        """,
        session_row["order_id"],
    )
    if already_paid:
        await conn.execute(
            """
            UPDATE tenant_collection_sessions
            SET status = 'approved',
                updated_at = NOW()
            WHERE id = $1 AND status = 'pending'
            """,
            session_row["id"],
        )
        await conn.execute(
            """
            UPDATE tenant_collection_sessions
            SET status = 'voided',
                updated_at = NOW()
            WHERE order_id = $2
              AND id <> $1
              AND status = 'pending'
            """,
            session_row["id"],
            session_row["order_id"],
        )
        return {
            "applied": False,
            "idempotent": True,
            "orderPaymentId": str(session_row["order_payment_id"]) if session_row["order_payment_id"] else None,
        }
    merchant = await _load_merchant(conn, tenant_id)
    pay_amount = amount if amount is not None else Decimal(str(session_row["amount"]))
    payment = await conn.fetchrow(
        """
        INSERT INTO order_payments
            (order_id, tenant_id, amount, payment_method, payment_method_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        session_row["order_id"],
        tenant_id,
        pay_amount,
        DIGITAL_SLUG,
        merchant["payment_method_id"],
    )
    await conn.execute(
        """
        UPDATE tenant_collection_sessions
        SET status = 'approved',
            provider_tx_id = $2,
            order_payment_id = $3,
            provider_payload = COALESCE($4, provider_payload),
            provider_payment_method_type = COALESCE($5, provider_payment_method_type),
            customer_email = COALESCE($6, customer_email),
            currency = COALESCE($7, currency),
            environment = COALESCE($8, environment),
            updated_at = NOW()
        WHERE id = $1
        """,
        session_row["id"],
        provider_tx_id,
        payment["id"],
        json.dumps(_jsonable(provider_payload)) if provider_payload else None,
        (provider_payload or {}).get("payment_method_type"),
        (provider_payload or {}).get("customer_email"),
        (provider_payload or {}).get("currency"),
        (provider_payload or {}).get("environment"),
    )
    await conn.execute(
        """
        UPDATE orders
        SET status = 'completed',
            payment_status = 'paid',
            payment_method = $2,
            payment_method_id = $3
        WHERE id = $1
        """,
        session_row["order_id"],
        DIGITAL_SLUG,
        merchant["payment_method_id"],
    )
    try:
        await _post_approved_collection_gl(
            conn,
            tenant_id=tenant_id,
            order_id=session_row["order_id"],
            payment_method=DIGITAL_SLUG,
            payment_method_id=merchant["payment_method_id"],
        )
    except Exception as exc:
        logger.error(
            "GL entry failed for Wompi collection order %s: %s",
            session_row["order_id"],
            exc,
        )
    channel = "tenant_" + str(tenant_id).replace("-", "")
    notify_payload = {
        "type": NOTIFY_TYPE,
        "order_id": str(session_row["order_id"]),
        "session_id": str(session_row["id"]),
        "order_payment_id": str(payment["id"]),
    }
    await conn.execute("SELECT pg_notify($1, $2)", channel, json.dumps(notify_payload))
    return {
        "applied": True,
        "idempotent": False,
        "orderPaymentId": str(payment["id"]),
    }


async def apply_from_transaction(
    conn,
    *,
    tenant_id: UUID,
    session_row: Any,
    transaction: Dict[str, Any],
) -> dict:
    status = str(transaction.get("status") or "").upper()
    tx_id = str(transaction.get("id") or "")
    if not tx_id:
        raise ValidationError("Transacción Wompi sin id")
    if status != "APPROVED":
        await conn.execute(
            """
            UPDATE tenant_collection_sessions
            SET status = $2,
                provider_tx_id = $3,
                provider_payload = $4,
                provider_payment_method_type = $5,
                customer_email = $6,
                currency = $7,
                environment = COALESCE($8, environment),
                updated_at = NOW()
            WHERE id = $1 AND status = 'pending'
            """,
            session_row["id"],
            status.lower(),
            tx_id,
            json.dumps(_jsonable(transaction)),
            transaction.get("payment_method_type"),
            transaction.get("customer_email"),
            transaction.get("currency"),
            transaction.get("environment"),
        )
        return {"applied": False, "idempotent": False, "status": status}
    cents = transaction.get("amount_in_cents")
    amount = Decimal(cents) / Decimal(100) if cents is not None else None
    return await apply_approved_payment(
        conn,
        tenant_id=tenant_id,
        session_row=session_row,
        provider_tx_id=tx_id,
        amount=amount,
        provider_payload=transaction,
    )


async def fetch_transaction(private_key: str, environment: str, transaction_id: str) -> dict:
    url = f"{_wompi_base_url(environment)}/transactions/{transaction_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=restaurant_headers(private_key))
    except httpx.RequestError as exc:
        logger.error("Wompi get_transaction connection error")
        raise ValidationError("No se pudo consultar la transacción en Wompi") from exc
    if response.status_code >= 400:
        raise ValidationError("Wompi no encontró la transacción")
    try:
        body = response.json()
    except ValueError as exc:
        raise ValidationError("Wompi no encontró la transacción") from exc
    data = wompi_resource_data(body)
    if isinstance(data, list):
        first = data[0] if data else {}
        return first if isinstance(first, dict) else {}
    return data if isinstance(data, dict) else {}


def _pick_referenced_transaction(rows: Any) -> Optional[dict]:
    items = [row for row in list(rows or []) if isinstance(row, dict)]
    for row in items:
        if str(row.get("status") or "").upper() == "APPROVED":
            return row
    return items[0] if items else None


async def fetch_transaction_by_reference(
    private_key: str, environment: str, reference: str
) -> Optional[dict]:
    url = f"{_wompi_base_url(environment)}/transactions"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                url,
                headers=restaurant_headers(private_key),
                params={"reference": reference},
            )
    except httpx.RequestError as exc:
        logger.error("Wompi list_transactions connection error")
        raise ValidationError("No se pudo consultar la transacción en Wompi") from exc
    if response.status_code >= 400:
        raise ValidationError("Wompi no encontró la transacción")
    try:
        body = response.json()
    except ValueError as exc:
        raise ValidationError("Wompi no encontró la transacción") from exc
    data = wompi_resource_data(body)
    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return _pick_referenced_transaction(rows)


async def verify_session(session_id: UUID, transaction_id: Optional[str] = None) -> dict:
    async with get_db_connection(use_transaction=True) as conn:
        session_row = await conn.fetchrow(
            """
            SELECT * FROM tenant_collection_sessions WHERE id = $1
            FOR UPDATE
            """,
            session_id,
        )
        if not session_row:
            raise NotFoundError("Sesión de cobro no encontrada")
        tenant_id = session_row["tenant_id"]
        merchant = await _load_merchant(conn, tenant_id)
        private_key = await openbao_transit.decrypt_ciphertext(
            merchant["private_key_ciphertext"]
        )
        tx_id = transaction_id or session_row["provider_tx_id"]
        if tx_id:
            transaction = await fetch_transaction(
                private_key, merchant["environment"], tx_id
            )
        else:
            transaction = await fetch_transaction_by_reference(
                private_key, merchant["environment"], str(session_id)
            )
            if not transaction:
                return {
                    "success": True,
                    "data": {"status": session_row["status"], "applied": False},
                }
        result = await apply_from_transaction(
            conn,
            tenant_id=tenant_id,
            session_row=session_row,
            transaction=transaction,
        )
        result["status"] = transaction.get("status")
        return {"success": True, "data": result}


async def handle_collections_webhook(event_data: Dict[str, Any]) -> dict:
    transaction = (event_data.get("data") or {}).get("transaction") or {}
    reference = transaction.get("reference")
    tx_id = transaction.get("id")
    if not reference:
        return {"ok": True, "ignored": True}
    try:
        session_id = UUID(str(reference))
    except (ValueError, TypeError):
        return {"ok": True, "ignored": True}

    async with get_db_connection(use_transaction=True) as conn:
        session_row = await conn.fetchrow(
            """
            SELECT * FROM tenant_collection_sessions WHERE id = $1
            FOR UPDATE
            """,
            session_id,
        )
        if not session_row:
            return {"ok": True, "ignored": True}
        tenant_id = session_row["tenant_id"]
        merchant = await _load_merchant(conn, tenant_id)
        events_secret = await openbao_transit.decrypt_ciphertext(
            merchant["events_secret_ciphertext"]
        )
        if not verify_event_signature_with_secret(
            event_data, events_secret, merchant["environment"]
        ):
            raise ValidationError("Firma Wompi inválida")
        if not tx_id:
            return {"ok": True, "ignored": True}
        result = await apply_from_transaction(
            conn,
            tenant_id=tenant_id,
            session_row=session_row,
            transaction=transaction,
        )
        return {"ok": True, "data": result}
