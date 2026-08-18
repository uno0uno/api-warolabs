"""Restaurant Wompi diner collections (#862). Never uses server WOMPI_* keys."""
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

from app.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services import openbao_transit
from app.services.account_role_service import resolve_group_parent_account
from app.services.customers_service import ANONYMOUS_PHONE, GENERIC_CUSTOMER_EMAIL
from app.services.wompi_service import WOMPI_PRODUCTION_URL, WOMPI_SANDBOX_URL

logger = logging.getLogger(__name__)

WOMPI_METHOD_NAME = "Wompi"
DIGITAL_SLUG = "digital"
NOTIFY_TYPE = "order_payment_approved"
_THANK_YOU_HOSTS = {"warocol.com", "www.warocol.com", "localhost"}


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


def _matches_server_merchant(public_key: str, private_key: str) -> bool:
    server_pub = (settings.wompi_public_key or "").strip()
    server_prv = (settings.wompi_private_key or "").strip()
    pub = public_key.strip()
    prv = private_key.strip()
    if server_pub and pub == server_pub:
        return True
    if server_prv and prv == server_prv:
        return True
    return False


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
    data = (response.json() or {}).get("data") or {}
    remote_pub = (data.get("public_key") or "").strip()
    if remote_pub and remote_pub != public_key.strip():
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
        JOIN tenant_customers tc ON tc.customer_id = p.id AND tc.tenant_id = $1
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
    owned = await conn.fetchval(
        """
        SELECT 1 FROM tenant_customers
        WHERE tenant_id = $1 AND customer_id = $2
        """,
        tenant_id,
        selected_customer_id,
    )
    if not owned:
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
    if _matches_server_merchant(public_key, private_key):
        raise ValidationError("No se pueden usar las llaves Wompi de WARO/Tickets")
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
            INSERT INTO tenant_wompi_merchants (
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
            FROM tenant_wompi_merchants
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
        "SELECT * FROM tenant_wompi_merchants WHERE tenant_id = $1 AND is_active = true",
        tenant_id,
    )
    if not row:
        raise ValidationError("Pasarela Wompi no está activa")
    return row


async def public_collection_session(session_id: UUID) -> dict:
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT checkout_url, status
            FROM tenant_wompi_collection_sessions
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
        },
    }


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
    thank_you_url = _safe_thank_you_url(redirect_url, session_id)
    payload = {
        "name": f"WARO cobro {order_id}",
        "description": "Cobro al comensal (restaurante)",
        "single_use": True,
        "collect_shipping": False,
        "currency": "COP",
        "amount_in_cents": amount_cents,
        "expires_at": expiration.isoformat(),
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
        raise ValidationError("Wompi rechazó la creación del link de cobro")
    data = (response.json() or {}).get("data") or {}
    link_id = data.get("id")
    if not link_id:
        raise ValidationError("Wompi no devolvió link de cobro")
    checkout_url = f"https://checkout.wompi.co/l/{link_id}"
    await conn.execute(
        """
        INSERT INTO tenant_wompi_collection_sessions (
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
        return await _create_session_row(
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
        UPDATE tenant_wompi_collection_sessions
        SET status = 'approved',
            provider_tx_id = $2,
            order_payment_id = $3,
            updated_at = NOW()
        WHERE id = $1
        """,
        session_row["id"],
        provider_tx_id,
        payment["id"],
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
            UPDATE tenant_wompi_collection_sessions
            SET status = $2, provider_tx_id = $3, updated_at = NOW()
            WHERE id = $1 AND status = 'pending'
            """,
            session_row["id"],
            status.lower(),
            tx_id,
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
    return (response.json() or {}).get("data") or {}


async def verify_session(session_id: UUID, transaction_id: Optional[str] = None) -> dict:
    async with get_db_connection(use_transaction=True) as conn:
        session_row = await conn.fetchrow(
            """
            SELECT * FROM tenant_wompi_collection_sessions WHERE id = $1
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
        if not tx_id:
            return {
                "success": True,
                "data": {"status": session_row["status"], "applied": False},
            }
        transaction = await fetch_transaction(
            private_key, merchant["environment"], tx_id
        )
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
            SELECT * FROM tenant_wompi_collection_sessions WHERE id = $1
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
