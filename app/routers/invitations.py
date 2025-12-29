import logging
from fastapi import APIRouter, Request, Response
from app.services.invitation_service import (
    send_invitation,
    accept_invitation,
    get_pending_invitations,
    cancel_invitation
)
from app.models.invitation import (
    SendInvitationRequest,
    SendInvitationResponse,
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    PendingInvitationsResponse,
    CancelInvitationResponse
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/send", response_model=SendInvitationResponse)
async def send_invitation_endpoint(request: Request, payload: SendInvitationRequest):
    """
    Send team invitation email
    Requires admin or superuser role
    """
    return await send_invitation(request, payload)


@router.post("/accept", response_model=AcceptInvitationResponse)
async def accept_invitation_endpoint(request: Request, response: Response, payload: AcceptInvitationRequest):
    """
    Accept team invitation and create session
    Public endpoint - validates token
    """
    return await accept_invitation(request, response, payload.token)


@router.get("/pending", response_model=PendingInvitationsResponse)
async def get_pending_invitations_endpoint(request: Request):
    """
    Get pending invitations for current tenant
    Requires admin or superuser role
    """
    return await get_pending_invitations(request)


@router.delete("/{invitation_id}", response_model=CancelInvitationResponse)
async def cancel_invitation_endpoint(request: Request, invitation_id: str):
    """
    Cancel a pending invitation
    Requires admin or superuser role
    """
    return await cancel_invitation(request, invitation_id)
