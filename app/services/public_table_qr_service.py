"""Public Table QR — resolve, menu, submit (warocol.com#710).

api-warolabs#266: token resolve
api-warolabs#267: menu + pending request submit
"""
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, Request

from app.core.exceptions import NotFoundError
from app.database import get_db_connection
from app.services import notifications_service, payment_method_service
from app.services.online_cart_service import (
    validate_modifiers_for_item,
    validate_products_belong_to_tenant,
)
from app.services.public_restaurant_service import is_currently_open

logger = logging.getLogger(__name__)

_CONTEXT_SQL = """
    SELECT
        t.id AS table_id,
        t.tenant_id,
        t.name AS table_name,
        t.qr_enabled,
        t.is_active AS table_active,
        tpp.slug AS tenant_slug,
        tpp.display_name,
        tpp.table_qr_module_enabled,
        tpp.is_active AS profile_active,
        tpp.business_hours,
        tpp.is_manually_open
    FROM tables t
    JOIN tenant_public_profiles tpp ON tpp.tenant_id = t.tenant_id
    WHERE t.qr_public_token = $1
      AND t.deleted_at IS NULL
"""

_SUBMIT_LIMIT_PER_TOKEN = 10
_SUBMIT_LIMIT_PER_IP = 30
_SUBMIT_WINDOW_SECONDS = 300
_DUPLICATE_PENDING_WINDOW_MINUTES = 3
_submit_rate_buckets: Dict[str, List[float]] = defaultdict(list)


def _parse_business_hours(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(raw, dict):
        return raw
    return None


def _is_context_active(row: dict, is_open: bool) -> bool:
    return bool(
        row["table_qr_module_enabled"]
        and row["qr_enabled"]
        and row["profile_active"]
        and row["table_active"]
        and is_open
    )


async def _load_context_row(token: str) -> Optional[dict]:
    async with get_db_connection(use_transaction=False) as conn:
        return await conn.fetchrow(_CONTEXT_SQL, token)


async def resolve_table_qr_context(token: str) -> Optional[Dict[str, Any]]:
    """Internal context for menu/submit; None if token unknown or inactive."""
    row = await _load_context_row(token)
    if not row:
        return None

    business_hours = _parse_business_hours(row["business_hours"])
    is_open = is_currently_open(business_hours, bool(row["is_manually_open"]))
    if not _is_context_active(row, is_open):
        return None

    return {
        "tenant_id": row["tenant_id"],
        "table_id": row["table_id"],
        "tenant_slug": row["tenant_slug"],
        "display_name": row["display_name"],
        "table_name": row["table_name"],
        "is_currently_open": is_open,
    }


async def resolve_table_qr_token(token: str) -> Optional[Dict[str, Any]]:
    """Public metadata for GET /public/table-qr/{token} (#266)."""
    ctx = await resolve_table_qr_context(token)
    if not ctx:
        return None
    return {
        "tenant_slug": ctx["tenant_slug"],
        "display_name": ctx["display_name"],
        "table_name": ctx["table_name"],
        "table_qr_module_enabled": True,
        "qr_enabled": True,
        "is_currently_open": ctx["is_currently_open"],
    }


def _check_submit_rate_limit(*, token: str, client_ip: Optional[str]) -> None:
    now = time.time()
    window_start = now - _SUBMIT_WINDOW_SECONDS

    for key in (f"token:{token}", f"ip:{client_ip or 'unknown'}"):
        limit = _SUBMIT_LIMIT_PER_TOKEN if key.startswith("token:") else _SUBMIT_LIMIT_PER_IP
        bucket = _submit_rate_buckets[key]
        bucket[:] = [ts for ts in bucket if ts > window_start]
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Demasiadas solicitudes. Intenta de nuevo en unos minutos.",
            )
        bucket.append(now)


async def _lock_table_qr_submit(conn, tenant_id: UUID, table_id: UUID) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))",
        str(tenant_id),
        str(table_id),
    )


async def _find_matching_pending_request(
    conn,
    tenant_id: UUID,
    table_id: UUID,
    items_json: str,
    payment_method: Optional[str],
    payment_method_id: Optional[UUID],
    customer_notes: Optional[str],
) -> Optional[dict]:
    return await conn.fetchrow(
        """
        SELECT id, created_at
        FROM table_qr_requests
        WHERE tenant_id = $1
          AND table_id = $2
          AND status = 'pending'
          AND items = $3::jsonb
          AND payment_method IS NOT DISTINCT FROM $4
          AND payment_method_id IS NOT DISTINCT FROM $5
          AND customer_notes IS NOT DISTINCT FROM $6
          AND created_at >= now() - ($7::text || ' minutes')::interval
        ORDER BY created_at DESC
        LIMIT 1
        """,
        tenant_id,
        table_id,
        items_json,
        payment_method,
        payment_method_id,
        customer_notes,
        _DUPLICATE_PENDING_WINDOW_MINUTES,
    )


async def get_menu_for_token(
    token: str,
    category_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    ctx = await resolve_table_qr_context(token)
    if not ctx:
        raise HTTPException(status_code=404, detail="QR link not found or inactive")

    tenant_id = ctx["tenant_id"]
    async with get_db_connection(use_transaction=False) as conn:
        categories_query = """
            SELECT DISTINCT c.id, c.name, c.description
            FROM categories c
            JOIN product p ON p.category_id = c.id
            WHERE p.tenant_id = $1
              AND p.is_available = true
              AND p.is_available_table_qr = true
            ORDER BY c.name
        """
        categories = [dict(r) for r in await conn.fetch(categories_query, tenant_id)]

        products_query = """
            SELECT
                p.id, p.name, p.description, p.price, p.image_url,
                p.category_id, c.name AS category_name,
                p.is_available, p.preparation_time,
                p.allow_modifiers,
                EXISTS(
                    SELECT 1 FROM product_modifier_groups pmg
                    WHERE pmg.product_id = p.id
                ) AS has_modifiers
            FROM product p
            JOIN categories c ON p.category_id = c.id
            WHERE p.tenant_id = $1
              AND p.is_available = true
              AND p.is_available_table_qr = true
        """
        params: List[Any] = [tenant_id]
        if category_id:
            products_query += " AND p.category_id = $2"
            params.append(category_id)
        products_query += " ORDER BY c.name, p.name"

        products = [dict(r) for r in await conn.fetch(products_query, *params)]
        for p in products:
            p["price"] = float(p["price"])

    return {
        "restaurant_name": ctx["display_name"],
        "table_name": ctx["table_name"],
        "is_currently_open": ctx["is_currently_open"],
        "categories": categories,
        "products": products,
    }


async def get_product_detail_for_token(token: str, product_id: UUID) -> Dict[str, Any]:
    ctx = await resolve_table_qr_context(token)
    if not ctx:
        raise HTTPException(status_code=404, detail="QR link not found or inactive")

    tenant_id = ctx["tenant_id"]
    async with get_db_connection(use_transaction=False) as conn:
        product_row = await conn.fetchrow(
            """
            SELECT
                p.id, p.name, p.description, p.price, p.image_url,
                c.name AS category_name,
                p.is_available, p.is_available_table_qr, p.preparation_time
            FROM product p
            JOIN categories c ON p.category_id = c.id
            WHERE p.id = $1
              AND p.tenant_id = $2
              AND p.is_available = true
              AND p.is_available_table_qr = true
            """,
            product_id,
            tenant_id,
        )
        if not product_row:
            raise HTTPException(status_code=404, detail="Product not found")

        product = dict(product_row)
        product["price"] = float(product["price"])

        modifiers_rows = await conn.fetch(
            """
            SELECT
                mg.id AS group_id,
                mg.name AS group_name,
                mg.is_required,
                mg.min_qty,
                mg.max_qty,
                mg.sort_order AS group_sort_order,
                m.id AS modifier_id,
                m.name AS modifier_name,
                m.price AS modifier_price,
                m.is_available AS modifier_is_available,
                m.is_default AS modifier_is_default,
                m.max_limit AS modifier_max_limit,
                m.sort_order AS modifier_sort_order,
                m.option_type AS modifier_option_type
            FROM product_modifier_groups pmg
            JOIN modifier_groups mg ON mg.id = pmg.modifier_group_id
            LEFT JOIN modifiers m ON m.modifier_group_id = mg.id
            WHERE pmg.product_id = $1
            ORDER BY mg.sort_order, m.sort_order
            """,
            product_id,
        )

    modifier_groups: Dict[str, dict] = {}
    for row in modifiers_rows:
        group_id = str(row["group_id"])
        if group_id not in modifier_groups:
            modifier_groups[group_id] = {
                "id": row["group_id"],
                "name": row["group_name"],
                "is_required": row["is_required"],
                "min_qty": row["min_qty"],
                "max_qty": row["max_qty"],
                "modifiers": [],
            }
        if row["modifier_id"] and row["modifier_is_available"]:
            modifier_groups[group_id]["modifiers"].append({
                "id": row["modifier_id"],
                "name": row["modifier_name"],
                "price": float(row["modifier_price"]),
                "is_available": row["modifier_is_available"],
                "is_default": row["modifier_is_default"],
                "max_limit": row["modifier_max_limit"],
                "option_type": row["modifier_option_type"] or "INGREDIENT",
            })

    product["modifier_groups"] = list(modifier_groups.values())
    return product


async def get_payment_methods_for_token(token: str) -> dict:
    ctx = await resolve_table_qr_context(token)
    if not ctx:
        raise HTTPException(status_code=404, detail="QR link not found or inactive")
    try:
        return await payment_method_service.list_public_methods_by_tenant_slug(ctx["tenant_slug"])
    except NotFoundError:
        raise HTTPException(status_code=404, detail="QR link not found or inactive") from None


async def _validate_payment_selection(
    conn,
    tenant_id: UUID,
    payment_method: Optional[str],
    payment_method_id: Optional[UUID],
) -> None:
    if not payment_method:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "payment_method_required",
                "message": "Selecciona un método de pago.",
            },
        )

    group_row = await conn.fetchrow(
        """
        SELECT id, triggers_cartera FROM payment_method_groups
        WHERE slug = $1
          AND is_active = true
          AND (tenant_id IS NULL OR tenant_id = $2)
        """,
        payment_method,
        tenant_id,
    )
    if not group_row:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "payment_method_invalid",
                "message": f"Método de pago '{payment_method}' no es válido para este restaurante.",
            },
        )
    if group_row["triggers_cartera"]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "payment_method_not_allowed",
                "message": "Este método de pago no está disponible para pedidos por QR.",
            },
        )

    if payment_method_id:
        method_row = await conn.fetchrow(
            """
            SELECT id FROM payment_methods
            WHERE id = $1
              AND tenant_id = $2
              AND group_id = $3
              AND is_active = true
            """,
            payment_method_id,
            tenant_id,
            group_row["id"],
        )
        if not method_row:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "payment_method_id_invalid",
                    "message": "El método seleccionado no pertenece al grupo elegido.",
                },
            )


async def _build_item_snapshots(
    conn,
    tenant_id: UUID,
    items: List[dict],
) -> tuple[List[dict], Decimal]:
    if not items:
        raise HTTPException(status_code=400, detail="El pedido debe incluir al menos un producto.")

    product_ids = [UUID(str(item["product_id"])) for item in items]
    await validate_products_belong_to_tenant(conn, product_ids, tenant_id)

    product_rows = await conn.fetch(
        """
        SELECT id, name, price
        FROM product
        WHERE id = ANY($1::uuid[])
          AND tenant_id = $2
          AND is_available = true
          AND is_available_table_qr = true
        """,
        product_ids,
        tenant_id,
    )
    if len(product_rows) != len(set(product_ids)):
        raise HTTPException(
            status_code=409,
            detail="Uno o más productos ya no están disponibles para pedido por QR.",
        )

    prices = {row["id"]: Decimal(str(row["price"])) for row in product_rows}
    snapshots: List[dict] = []
    total = Decimal("0")

    for item in items:
        product_id = UUID(str(item["product_id"]))
        quantity = int(item["quantity"])
        if quantity < 1:
            raise HTTPException(status_code=400, detail="La cantidad debe ser al menos 1.")

        modifiers_in = item.get("modifiers") or []
        if modifiers_in:
            await validate_modifiers_for_item(conn, product_id, modifiers_in)

        unit_price = prices[product_id]
        resolved_modifiers: List[dict] = []
        modifier_total = Decimal("0")

        if modifiers_in:
            modifier_ids = [UUID(str(m["id"])) for m in modifiers_in]
            mod_rows = await conn.fetch(
                "SELECT id, name, price FROM modifiers WHERE id = ANY($1::uuid[])",
                modifier_ids,
            )
            mod_map = {row["id"]: row for row in mod_rows}
            for mod in modifiers_in:
                mod_id = UUID(str(mod["id"]))
                mod_quantity = int(mod.get("quantity", 1))
                if mod_quantity < 1:
                    raise HTTPException(
                        status_code=400,
                        detail="La cantidad del modificador debe ser al menos 1.",
                    )
                db_mod = mod_map.get(mod_id)
                if not db_mod:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Modifier '{mod_id}' does not exist or is not available",
                    )
                price = Decimal(str(db_mod["price"]))
                modifier_total += price * mod_quantity
                resolved_modifiers.append({
                    "id": str(mod_id),
                    "name": db_mod["name"],
                    "price": float(price),
                    "quantity": mod_quantity,
                })

        line_total = (unit_price + modifier_total) * quantity
        total += line_total

        snapshots.append({
            "product_id": str(product_id),
            "quantity": quantity,
            "unit_price": float(unit_price),
            "modifiers": resolved_modifiers,
            "notes": item.get("notes"),
            "line_total": float(line_total),
        })

    return snapshots, total


async def submit_table_qr_request(
    request: Request,
    token: str,
    items: List[dict],
    payment_method: Optional[str],
    payment_method_id: Optional[UUID],
    customer_notes: Optional[str],
) -> Dict[str, Any]:
    """Create a pending table_qr_requests row — no orders, tab, or comandas (#267)."""
    ctx = await resolve_table_qr_context(token)
    if not ctx:
        raise HTTPException(status_code=404, detail="QR link not found or inactive")

    if not ctx["is_currently_open"]:
        raise HTTPException(
            status_code=409,
            detail="El restaurante está cerrado en este momento. No se pueden enviar pedidos.",
        )

    client_ip = request.client.host if request.client else None
    _check_submit_rate_limit(token=token, client_ip=client_ip)

    tenant_id = ctx["tenant_id"]
    table_id = ctx["table_id"]

    async with get_db_connection() as conn:
        async with conn.transaction():
            item_snapshots, total_amount = await _build_item_snapshots(conn, tenant_id, items)
            await _validate_payment_selection(conn, tenant_id, payment_method, payment_method_id)
            items_json = json.dumps(item_snapshots, sort_keys=True)

            await _lock_table_qr_submit(conn, tenant_id, table_id)
            existing = await _find_matching_pending_request(
                conn,
                tenant_id,
                table_id,
                items_json,
                payment_method,
                payment_method_id,
                customer_notes,
            )
            if existing:
                request_id = existing["id"]
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO table_qr_requests (
                        tenant_id, table_id, status, items,
                        payment_method, payment_method_id, customer_notes
                    )
                    VALUES ($1, $2, 'pending', $3::jsonb, $4, $5, $6)
                    RETURNING id, created_at
                    """,
                    tenant_id,
                    table_id,
                    items_json,
                    payment_method,
                    payment_method_id,
                    customer_notes,
                )

                request_id = row["id"]
                payload = {
                    "type": "table_qr_request",
                    "request_id": str(request_id),
                    "table_id": str(table_id),
                    "table_name": ctx["table_name"],
                    "item_count": len(item_snapshots),
                    "total_amount": float(total_amount),
                }
                try:
                    await notifications_service.create_table_qr_notification(
                        conn, tenant_id, request_id, payload
                    )
                except Exception as err:
                    logger.warning("Table QR notification failed for %s: %s", request_id, err)

    return {
        "request_id": str(request_id),
        "status": "pending",
        "table_name": ctx["table_name"],
        "total_amount": float(total_amount),
        "message": "Pedido recibido — el restaurante lo confirmará.",
    }
