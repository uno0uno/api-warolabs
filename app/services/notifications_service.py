"""
Notifications Service
Tenant-scoped notification management for restaurant operators.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from uuid import NAMESPACE_URL, uuid5
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError
from app.services import billing_service, legal_service

logger = logging.getLogger(__name__)
TERMS_ACCEPTANCE_REQUIRED_TYPE = "terms_acceptance_required"


async def create_order_notification(conn, tenant_id, order_id, payload: dict):
    """
    Insert a notification row using an existing connection (shares caller's transaction).
    Does NOT call get_db_connection() — conn must be provided by caller.
    """
    await conn.execute(
        """INSERT INTO notifications (tenant_id, order_id, type, payload)
           VALUES ($1, $2, 'new_order', $3::jsonb)""",
        tenant_id, order_id, json.dumps(payload)
    )
    # Real-time push via pg_notify (same conn = same transaction, fires on commit)
    channel = "tenant_" + str(tenant_id).replace("-", "")
    await conn.execute("SELECT pg_notify($1, $2)", channel, json.dumps(payload))


async def create_comanda_ready_notification(
    conn, tenant_id: UUID, order_id: UUID, payload: dict
) -> None:
    """Notify POS/expediter when a comanda reaches ready (KDS or monitor)."""
    await conn.execute(
        """INSERT INTO notifications (tenant_id, order_id, type, payload)
           VALUES ($1, $2, 'comanda_ready', $3::jsonb)""",
        tenant_id,
        order_id,
        json.dumps(payload),
    )
    channel = "tenant_" + str(tenant_id).replace("-", "")
    notify_payload = {**payload, "type": "comanda_ready"}
    await conn.execute("SELECT pg_notify($1, $2)", channel, json.dumps(notify_payload))


async def create_table_qr_notification(
    conn, tenant_id: UUID, request_id: UUID, payload: dict
) -> None:
    """Notify staff of a pending Table QR request (api-warolabs#267). order_id is NULL."""
    await conn.execute(
        """INSERT INTO notifications (tenant_id, order_id, type, payload)
           VALUES ($1, NULL, 'table_qr_request', $2::jsonb)""",
        tenant_id,
        json.dumps(payload),
    )
    channel = "tenant_" + str(tenant_id).replace("-", "")
    notify_payload = {**payload, "request_id": str(request_id)}
    await conn.execute("SELECT pg_notify($1, $2)", channel, json.dumps(notify_payload))


async def get_unread_notifications(request: Request) -> dict:
    """GET /notifications — returns last 50 unread for the tenant."""
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            terms_notification = await _build_terms_acceptance_notification(conn, tenant_id)
            rows = await conn.fetch(
                """SELECT id, order_id, type, payload, created_at
                   FROM notifications
                   WHERE tenant_id = $1 AND read_at IS NULL
                   ORDER BY created_at DESC
                   LIMIT 50""",
                tenant_id
            )
            return {
                "success": True,
                "data": ([terms_notification] if terms_notification else []) + [
                    {
                        "id": str(r["id"]),
                        "order_id": str(r["order_id"]) if r["order_id"] else None,
                        "type": r["type"],
                        "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"]),
                        "created_at": r["created_at"].isoformat(),
                    }
                    for r in rows
                ]
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting unread notifications: {str(e)}")
        raise APIError(f"Error getting notifications: {str(e)}", status_code=500)


async def mark_notification_read(request: Request, notification_id: UUID) -> dict:
    """PATCH /notifications/{id}/read — marks one notification as read."""
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """UPDATE notifications SET read_at = NOW()
                   WHERE id = $1 AND tenant_id = $2 AND read_at IS NULL
                   RETURNING id""",
                notification_id, tenant_id
            )
            if not row:
                raise NotFoundError(f"Notification {notification_id} not found or already read")
            return {"success": True}

    except (AuthenticationError, NotFoundError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error marking notification {notification_id} as read: {str(e)}")
        raise APIError(f"Error marking notification as read: {str(e)}", status_code=500)


async def mark_all_notifications_read(request: Request) -> dict:
    """POST /notifications/read-all — marks all unread as read."""
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            result = await conn.execute(
                "UPDATE notifications SET read_at = NOW() WHERE tenant_id = $1 AND read_at IS NULL",
                tenant_id
            )
            count = int(result.split()[-1])  # "UPDATE N"
            return {"success": True, "data": {"marked_read": count}}

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error marking all notifications as read: {str(e)}")
        raise APIError(f"Error marking all notifications as read: {str(e)}", status_code=500)


async def _build_terms_acceptance_notification(conn, tenant_id: UUID) -> Optional[dict]:
    """
    Return a virtual unread notification for paid tenants pending current TyC.

    It is intentionally not persisted: the reminder is derived from legal and
    subscription state, so it remains visible after "mark all read" until the
    tenant accepts the current document version.
    """
    access = await billing_service.get_subscription_access(tenant_id, conn)
    if access.subscription_status not in {"active", "past_due", "pending"}:
        return None

    status = await legal_service.get_terms_status(conn, tenant_id)
    data = status.get("data") or {}
    if not data.get("requires_acceptance"):
        return None

    current = data.get("current") or {}
    version_id = current.get("version_id")
    version = current.get("version")
    notification_id = uuid5(
        NAMESPACE_URL,
        f"waro:terms_acceptance_required:{tenant_id}:{version_id or version or 'current'}",
    )
    return {
        "id": str(notification_id),
        "order_id": None,
        "type": TERMS_ACCEPTANCE_REQUIRED_TYPE,
        "payload": {
            "document_version_id": version_id,
            "version": version,
            "return_to": "/gestion/billing",
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
