"""
Notifications Router
Authenticated endpoints for restaurant operators to manage notifications.
"""
import asyncio
import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from uuid import UUID
from app.config import settings
from app.core.middleware import require_valid_session
from app.services import notifications_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/stream")
async def notification_stream(request: Request):
    """
    SSE endpoint — keeps connection open and pushes new order notifications in real time.
    Uses a dedicated asyncpg connection (outside the pool) for LISTEN.
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant ID not found in session")

    channel = "tenant_" + str(tenant_id).replace("-", "")
    queue: asyncio.Queue = asyncio.Queue()

    async def on_notify(connection, pid, channel_name, payload_str):
        await queue.put(payload_str)

    async def event_stream():
        conn = await asyncpg.connect(dsn=settings.database_url)
        await conn.add_listener(channel, on_notify)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload_str = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {payload_str}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await conn.remove_listener(channel, on_notify)
            await conn.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("")
async def get_notifications(request: Request):
    """
    List last 50 unread notifications for the authenticated tenant.

    **Requires valid session cookie.**
    """
    return await notifications_service.get_unread_notifications(request)


@router.patch("/{notification_id}/read")
async def mark_read(request: Request, notification_id: UUID):
    """
    Mark a single notification as read.

    Returns 404 if not found or already read.

    **Requires valid session cookie.**
    """
    return await notifications_service.mark_notification_read(request, notification_id)


@router.post("/read-all")
async def mark_all_read(request: Request):
    """
    Mark all unread notifications as read for the authenticated tenant.

    Returns the count of notifications marked as read.

    **Requires valid session cookie.**
    """
    return await notifications_service.mark_all_notifications_read(request)
