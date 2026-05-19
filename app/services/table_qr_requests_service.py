"""Table QR request accept/reject for staff (api-warolabs#268, warocol.com#710)."""
import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request

from app.core.exceptions import APIError, AuthenticationError, NotFoundError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services.tables_service import _add_tab_items_core, fire_table_items

logger = logging.getLogger(__name__)


def _parse_items_json(raw: Any) -> List[dict]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


def _format_request_row(row: dict) -> dict:
    items = _parse_items_json(row["items"])
    return {
        "id": str(row["id"]),
        "table_id": str(row["table_id"]),
        "table_name": row.get("table_name"),
        "status": row["status"],
        "items": items,
        "item_count": len(items),
        "payment_method": row["payment_method"],
        "payment_method_id": str(row["payment_method_id"]) if row.get("payment_method_id") else None,
        "customer_notes": row.get("customer_notes"),
        "created_at": row["created_at"].isoformat(),
    }


async def _resolve_tenant_member_id(conn, tenant_id: UUID, user_id: UUID) -> Optional[UUID]:
    return await conn.fetchval(
        """
        SELECT id FROM tenant_members
        WHERE tenant_id = $1 AND user_id = $2
          AND is_active = true AND terminated_at IS NULL
        """,
        tenant_id,
        user_id,
    )


async def _ensure_open_session_in_tx(
    conn, table_id: UUID, tenant_id: UUID, user_id: UUID
) -> UUID:
    """Open a table session if none exists (same tx as accept)."""
    existing = await conn.fetchrow(
        """
        SELECT id FROM table_sessions
        WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL
        """,
        table_id,
        tenant_id,
    )
    if existing:
        return existing["id"]

    table_row = await conn.fetchrow(
        """
        SELECT id, status FROM tables
        WHERE id = $1 AND tenant_id = $2 AND is_active = true
        FOR UPDATE
        """,
        table_id,
        tenant_id,
    )
    if not table_row:
        raise NotFoundError("Table not found")

    session_row = await conn.fetchrow(
        """
        INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        table_id,
        tenant_id,
        user_id,
    )
    await conn.execute(
        "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
        table_id,
        tenant_id,
    )
    return session_row["id"]


async def list_pending_grouped(request: Request) -> dict:
    """GET /table-qr-requests?status=pending — grouped by table for Despacho."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT
                r.id, r.table_id, r.status, r.items,
                r.payment_method, r.payment_method_id, r.customer_notes, r.created_at,
                t.name AS table_name
            FROM table_qr_requests r
            JOIN tables t ON t.id = r.table_id
            WHERE r.tenant_id = $1 AND r.status = 'pending'
            ORDER BY t.name, r.created_at ASC
            """,
            tenant_id,
        )

    tables_map: Dict[str, dict] = {}
    for row in rows:
        table_id = str(row["table_id"])
        if table_id not in tables_map:
            tables_map[table_id] = {
                "table_id": table_id,
                "table_name": row["table_name"],
                "requests": [],
            }
        tables_map[table_id]["requests"].append(_format_request_row(dict(row)))

    return {
        "success": True,
        "data": {
            "tables": list(tables_map.values()),
            "total_pending": len(rows),
        },
    }


async def reject_request(request: Request, request_id: UUID) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE table_qr_requests
            SET status = 'rejected', rejected_at = now()
            WHERE id = $1 AND tenant_id = $2 AND status = 'pending'
            RETURNING id, table_id
            """,
            request_id,
            tenant_id,
        )
        if not row:
            raise APIError(
                "Request not found or not pending",
                status_code=404,
            )

    return {
        "success": True,
        "data": {
            "request_id": str(row["id"]),
            "status": "rejected",
        },
    }


async def _load_pending_for_accept(
    conn,
    tenant_id: UUID,
    request_ids: List[UUID],
) -> List[dict]:
    rows = await conn.fetch(
        """
        SELECT
            id, table_id, status, items,
            payment_method, payment_method_id, customer_notes, created_at
        FROM table_qr_requests
        WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND status = 'pending'
        ORDER BY created_at ASC
        FOR UPDATE
        """,
        tenant_id,
        request_ids,
    )
    if len(rows) != len(set(request_ids)):
        raise APIError(
            "One or more requests are missing or not pending",
            status_code=409,
        )
    return [dict(r) for r in rows]


async def accept_requests(
    request: Request,
    request_ids: List[UUID],
) -> dict:
    """
    Accept pending Table QR requests: open session, one tab add, mark accepted.
    All request_ids must belong to the same table.
    """
    if not request_ids:
        raise APIError("No request IDs provided", status_code=400)

    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id or not user_id:
        raise AuthenticationError("Tenant ID is required")

    table_id: Optional[UUID] = None
    tab_result: dict = {}
    accepted_ids: List[UUID] = []
    payment_method: Optional[str] = None
    payment_method_id: Optional[UUID] = None

    async with get_db_connection() as conn:
        async with conn.transaction():
            pending_rows = await _load_pending_for_accept(conn, tenant_id, request_ids)

            table_ids = {row["table_id"] for row in pending_rows}
            if len(table_ids) != 1:
                raise APIError(
                    "All requests must belong to the same table",
                    status_code=409,
                )
            table_id = pending_rows[0]["table_id"]

            payment_methods = {row["payment_method"] for row in pending_rows if row["payment_method"]}
            if len(payment_methods) > 1:
                raise APIError(
                    "Conflicting payment methods across selected requests",
                    status_code=409,
                )
            payment_method = pending_rows[0]["payment_method"]
            payment_method_id = pending_rows[0]["payment_method_id"]

            member_id = await _resolve_tenant_member_id(conn, tenant_id, user_id)
            session_id = await _ensure_open_session_in_tx(conn, table_id, tenant_id, user_id)

            merged_items: List[dict] = []
            for row in pending_rows:
                merged_items.extend(_parse_items_json(row["items"]))

            if not merged_items:
                raise APIError("No items to accept", status_code=400)

            tab_result = await _add_tab_items_core(
                conn, tenant_id, user_id, table_id, merged_items
            )

            if payment_method:
                await conn.execute(
                    """
                    UPDATE orders
                    SET payment_method = $1, payment_method_id = $2
                    WHERE table_session_id = $3 AND status = 'pending'
                    """,
                    payment_method,
                    payment_method_id,
                    tab_result["session_id"],
                )

            updated = await conn.fetch(
                """
                UPDATE table_qr_requests
                SET status = 'accepted',
                    session_id = $1,
                    accepted_by = $2,
                    accepted_at = now()
                WHERE tenant_id = $3
                  AND id = ANY($4::uuid[])
                  AND status = 'pending'
                RETURNING id
                """,
                session_id,
                member_id,
                tenant_id,
                request_ids,
            )
            accepted_ids = [r["id"] for r in updated]
            if len(accepted_ids) != len(request_ids):
                raise APIError("Failed to mark all requests as accepted", status_code=500)

    try:
        await fire_table_items(request, table_id)
    except Exception as err:
        logger.error("Table QR accept auto-fire failed for table %s: %s", table_id, err)

    return {
        "success": True,
        "data": {
            "accepted_request_ids": [str(rid) for rid in accepted_ids],
            "table_id": str(table_id),
            "session_id": str(tab_result["session_id"]),
            "order_id": str(tab_result["order_id"]),
            "order_number": tab_result["order_number"],
            "items_count": tab_result["items_count"],
            "total_amount": tab_result["total_amount"],
            "payment_method": payment_method,
            "payment_method_id": str(payment_method_id) if payment_method_id else None,
        },
    }


async def bulk_accept(
    request: Request,
    request_ids: Optional[List[UUID]] = None,
    table_id: Optional[UUID] = None,
    all_pending: bool = False,
) -> dict:
    """Resolve request IDs then delegate to accept_requests."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    resolved_ids: List[UUID] = []

    if request_ids:
        resolved_ids = list(request_ids)
    elif table_id and all_pending:
        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM table_qr_requests
                WHERE tenant_id = $1 AND table_id = $2 AND status = 'pending'
                ORDER BY created_at ASC
                """,
                tenant_id,
                table_id,
            )
        resolved_ids = [r["id"] for r in rows]
        if not resolved_ids:
            raise APIError("No pending requests for this table", status_code=404)
    else:
        raise APIError(
            "Provide request_ids or table_id with all_pending=true",
            status_code=400,
        )

    return await accept_requests(request, resolved_ids)
