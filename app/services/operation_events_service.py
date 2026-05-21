"""Append-only POS operation audit events (warocol.com#782)."""
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, FrozenSet, List, Optional
from uuid import UUID

from fastapi import Request

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection

logger = logging.getLogger(__name__)

DOMAIN_POS = "pos"
CHANNELS: FrozenSet[str] = frozenset({"mesa", "barra", "mostrador"})
ACTIONS: FrozenSet[str] = frozenset({
    "tab_item_added",
    "tab_item_removed",
    "tab_item_qty_changed",
    "tab_cleared",
    "cart_line_removed",
    "cart_cleared",
    "payment_voided",
    "comanda_line_cancelled",
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
    channel: str,
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
    if domain not in (DOMAIN_POS,):
        logger.error("record_operation_event: invalid domain %s", domain)
        return
    if channel not in CHANNELS:
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
    domain: str = DOMAIN_POS,
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

    if domain != DOMAIN_POS:
        return {
            "success": True,
            "data": [],
            "pagination": {
                "total": 0,
                "limit": limit,
                "offset": offset,
                "has_more": False,
            },
        }

    where_conditions = ["e.tenant_id = $1", "e.domain = $2"]
    params: List[Any] = [tenant_id, domain]
    param_count = 2

    parsed_date_from = _parse_date(date_from)
    parsed_date_to = _parse_date(date_to)

    if parsed_date_from:
        param_count += 1
        where_conditions.append(
            f"e.created_at >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')"
        )
        params.append(parsed_date_from)

    if parsed_date_to:
        param_count += 1
        where_conditions.append(
            f"e.created_at < ((${param_count}::timestamp + interval '1 day') "
            f"AT TIME ZONE 'America/Bogota')"
        )
        params.append(parsed_date_to)

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
