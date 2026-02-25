"""
Notifications Service
Tenant-scoped notification management for restaurant operators.
"""
import json
import logging
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError

logger = logging.getLogger(__name__)


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


async def get_unread_notifications(request: Request) -> dict:
    """GET /notifications — returns last 50 unread for the tenant."""
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
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
                "data": [
                    {
                        "id": str(r["id"]),
                        "order_id": str(r["order_id"]) if r["order_id"] else None,
                        "type": r["type"],
                        "payload": dict(r["payload"]),
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
