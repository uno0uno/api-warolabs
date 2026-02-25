"""
Notifications Router
Authenticated endpoints for restaurant operators to manage notifications.
"""
from fastapi import APIRouter, Request
from uuid import UUID
from app.services import notifications_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


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
