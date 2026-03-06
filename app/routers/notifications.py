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

# Fan-out registry: one LISTEN connection per tenant channel, shared across all SSE clients.
# With 2 uvicorn workers each worker maintains its own registry (processes don't share memory).
# Worst case: 2 LISTEN connections per tenant (one per worker). Far better than 1 per tab.
_channel_listeners: dict = {}   # channel → set of asyncio.Queue
_channel_connections: dict = {} # channel → asyncpg.Connection
_channel_locks: dict = {}       # channel → asyncio.Lock (prevents double-connect race)


async def _ensure_listener(channel: str) -> None:
    """Open one LISTEN connection per channel. No-op if already open."""
    lock = _channel_locks.setdefault(channel, asyncio.Lock())
    async with lock:
        if channel not in _channel_connections:
            conn = await asyncpg.connect(dsn=settings.database_url)

            async def on_notify(conn, pid, channel_name, payload):
                for q in list(_channel_listeners.get(channel_name, set())):
                    await q.put(payload)

            await conn.add_listener(channel, on_notify)
            _channel_connections[channel] = conn


async def _remove_listener_if_empty(channel: str) -> None:
    """Close LISTEN connection and clean up registry when last client disconnects."""
    lock = _channel_locks.get(channel)
    if not lock:
        return
    async with lock:
        if not _channel_listeners.get(channel):
            conn = _channel_connections.pop(channel, None)
            if conn:
                await conn.close()
            _channel_listeners.pop(channel, None)
            _channel_locks.pop(channel, None)


@router.get("/stream")
async def notification_stream(request: Request):
    """
    SSE endpoint — keeps connection open and pushes new order notifications in real time.
    Uses a single shared asyncpg LISTEN connection per tenant channel, fan-out via asyncio queues.
    Multiple tabs of the same tenant share one PG connection within the same worker process.
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant ID not found in session")

    channel = "tenant_" + str(tenant_id).replace("-", "")
    queue: asyncio.Queue = asyncio.Queue()

    # Register queue BEFORE opening LISTEN to avoid losing events during setup race
    _channel_listeners.setdefault(channel, set()).add(queue)
    await _ensure_listener(channel)

    async def event_stream():
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
            _channel_listeners.get(channel, set()).discard(queue)
            await _remove_listener_if_empty(channel)

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
