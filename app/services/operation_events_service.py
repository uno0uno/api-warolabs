"""Append-only operation audit events for Bitácora (warocol.com#782 / #2323)."""
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, FrozenSet, List, Optional
from uuid import UUID

from fastapi import Request

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.core.timezones import resolve_tenant_timezone
from app.database import get_db_connection

logger = logging.getLogger(__name__)

DOMAIN_POS = "pos"
DOMAIN_MENU = "menu"
DOMAIN_ABASTECIMIENTO = "abastecimiento"
DOMAIN_EQUIPO = "equipo"
DOMAIN_INTEGRACIONES = "integraciones"
DOMAIN_MI_NEGOCIO = "mi_negocio"
DOMAINS: FrozenSet[str] = frozenset({
    "pos",
    "ventas",
    "despacho",
    "crm",
    "finanzas",
    "facturacion",
    "menu",
    "abastecimiento",
    "equipo",
    "integraciones",
    "mi_negocio",
})
CHANNELS: FrozenSet[str] = frozenset({"mesa", "barra", "mostrador"})
ACTIONS: FrozenSet[str] = frozenset({
    "tab_item_added",
    "tab_item_removed",
    "tab_item_qty_changed",
    "tab_item_edited",
    "tab_item_edit_blocked",
    "tab_cleared",
    "cart_line_removed",
    "cart_cleared",
    "payment_voided",
    "comanda_line_cancelled",
    "promotion_deleted",
    "product_created",
    "product_updated",
    "product_deleted",
    "modifier_group_created",
    "modifier_group_updated",
    "modifier_group_deleted",
    "recipe_created",
    "recipe_updated",
    "recipe_deleted",
    "menu_reordered",
    "purchase_created",
    "purchase_updated",
    "purchase_confirmed",
    "purchase_shipped",
    "purchase_received",
    "purchase_invoiced",
    "purchase_paid",
    "purchase_cancelled",
    "direct_purchase_created",
    "direct_purchase_updated",
    "direct_purchase_deleted",
    "stock_adjusted",
    "warehouse_category_created",
    "warehouse_category_updated",
    "warehouse_category_archived",
    "member_deleted",
    "member_role_updated",
    "invitation_sent",
    "invitation_cancelled",
    "role_override_updated",
    "role_override_deleted",
    "api_token_created",
    "api_token_updated",
    "api_token_revoked",
    "api_token_deleted",
    "public_profile_updated",
    "tax_config_updated",
    "financial_profile_updated",
})


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return value


def _to_jsonb(data: Any) -> str:
    serialized = _serialize_value(data if data is not None else {})
    return json.dumps(serialized)


def _parse_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_payload(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return dict(raw)
    return dict(raw)


async def record_operation_event(
    conn,
    tenant_id: UUID,
    *,
    domain: str,
    channel: Optional[str] = None,
    action: str,
    actor_user_id: Optional[UUID] = None,
    actor_member_id: Optional[UUID] = None,
    table_id: Optional[UUID] = None,
    table_session_id: Optional[UUID] = None,
    pos_cart_id: Optional[UUID] = None,
    order_id: Optional[UUID] = None,
    order_item_id: Optional[UUID] = None,
    comanda_item_id: Optional[UUID] = None,
    payload: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """Append one event in the caller's transaction. Never raises to the caller."""
    if domain not in DOMAINS:
        logger.error("record_operation_event: invalid domain %s", domain)
        return
    if channel is not None and channel not in CHANNELS:
        logger.error("record_operation_event: invalid channel %s", channel)
        return
    if action not in ACTIONS:
        logger.error("record_operation_event: invalid action %s", action)
        return

    try:
        await conn.execute(
            """
            INSERT INTO tenant_operation_events (
                tenant_id, domain, channel, action,
                actor_user_id, actor_member_id,
                table_id, table_session_id, pos_cart_id,
                order_id, order_item_id, comanda_item_id,
                payload, reason
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6,
                $7, $8, $9,
                $10, $11, $12,
                $13::jsonb, $14
            )
            """,
            tenant_id,
            domain,
            channel,
            action,
            actor_user_id,
            actor_member_id,
            table_id,
            table_session_id,
            pos_cart_id,
            order_id,
            order_item_id,
            comanda_item_id,
            _to_jsonb(payload),
            reason,
        )
    except Exception as exc:
        logger.error("record_operation_event failed: %s", exc)


async def record_module_event(
    conn,
    tenant_id: UUID,
    *,
    domain: str,
    action: str,
    actor_user_id: Optional[UUID] = None,
    entity_type: str,
    entity_id: Any = None,
    label: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """CUD helper: channel=None, payload entity_type/entity_id/label. Never raises."""
    payload: Dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "label": label,
    }
    if extra:
        payload.update(extra)
    await record_operation_event(
        conn,
        tenant_id,
        domain=domain,
        channel=None,
        action=action,
        actor_user_id=actor_user_id,
        payload=payload,
        reason=reason,
    )


def _row_to_event(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "created_at": row["created_at"].isoformat(),
        "domain": row["domain"],
        "channel": row["channel"],
        "action": row["action"],
        "actor_user_id": str(row["actor_user_id"]) if row["actor_user_id"] else None,
        "actor_user_name": row["actor_user_name"],
        "actor_member_id": str(row["actor_member_id"]) if row["actor_member_id"] else None,
        "actor_member_name": row["actor_member_name"],
        "table_id": str(row["table_id"]) if row["table_id"] else None,
        "table_session_id": str(row["table_session_id"]) if row["table_session_id"] else None,
        "pos_cart_id": str(row["pos_cart_id"]) if row["pos_cart_id"] else None,
        "order_id": str(row["order_id"]) if row["order_id"] else None,
        "order_item_id": str(row["order_item_id"]) if row["order_item_id"] else None,
        "comanda_item_id": str(row["comanda_item_id"]) if row["comanda_item_id"] else None,
        "payload": _parse_payload(row["payload"]),
        "reason": row["reason"],
    }


async def list_operation_events(
    request: Request,
    *,
    domain: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    channel: Optional[str] = None,
    action: Optional[str] = None,
    actor_user_id: Optional[UUID] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    where_conditions = ["e.tenant_id = $1"]
    params: List[Any] = [tenant_id]
    param_count = 1

    if domain:
        param_count += 1
        where_conditions.append(f"e.domain = ${param_count}")
        params.append(domain)

    parsed_date_from = _parse_date(date_from)
    parsed_date_to = _parse_date(date_to)

    async with get_db_connection(use_transaction=False) as conn:
        timezone_name = await resolve_tenant_timezone(conn, tenant_id)

    if parsed_date_from:
        param_count += 1
        date_param = param_count
        param_count += 1
        tz_param = param_count
        where_conditions.append(
            f"e.created_at >= (${date_param}::timestamp AT TIME ZONE ${tz_param})"
        )
        params.append(parsed_date_from)
        params.append(timezone_name)

    if parsed_date_to:
        param_count += 1
        date_param = param_count
        param_count += 1
        tz_param = param_count
        where_conditions.append(
            f"e.created_at < ((${date_param}::timestamp + interval '1 day') "
            f"AT TIME ZONE ${tz_param})"
        )
        params.append(parsed_date_to)
        params.append(timezone_name)

    if channel:
        param_count += 1
        where_conditions.append(f"e.channel = ${param_count}")
        params.append(channel)

    if action:
        param_count += 1
        where_conditions.append(f"e.action = ${param_count}")
        params.append(action)

    if actor_user_id:
        param_count += 1
        where_conditions.append(f"e.actor_user_id = ${param_count}")
        params.append(actor_user_id)

    if q and q.strip():
        param_count += 1
        where_conditions.append(f"e.payload::text ILIKE ${param_count}")
        params.append(f"%{q.strip()}%")

    where_clause = " AND ".join(where_conditions)

    async with get_db_connection(use_transaction=False) as conn:
        total_count = await conn.fetchval(
            f"SELECT COUNT(*) FROM tenant_operation_events e WHERE {where_clause}",
            *params,
        )

        limit_param = param_count + 1
        offset_param = param_count + 2
        rows = await conn.fetch(
            f"""
            SELECT
                e.id, e.tenant_id, e.created_at, e.domain, e.channel, e.action,
                e.actor_user_id, e.actor_member_id,
                e.table_id, e.table_session_id, e.pos_cart_id,
                e.order_id, e.order_item_id, e.comanda_item_id,
                e.payload, e.reason,
                pu.name AS actor_user_name,
                pm.name AS actor_member_name
            FROM tenant_operation_events e
            LEFT JOIN profile pu ON pu.id = e.actor_user_id
            LEFT JOIN tenant_members tm ON tm.id = e.actor_member_id
            LEFT JOIN profile pm ON pm.id = tm.user_id
            WHERE {where_clause}
            ORDER BY e.created_at DESC
            LIMIT ${limit_param} OFFSET ${offset_param}
            """,
            *params,
            limit,
            offset,
        )

    events = [_row_to_event(r) for r in rows]
    total = int(total_count or 0)

    return {
        "success": True,
        "data": events,
        "pagination": {
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total,
        },
    }
