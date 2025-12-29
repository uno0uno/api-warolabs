from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from enum import Enum


class InvitationRole(str, Enum):
    ADMIN = "admin"
    SUPERUSER = "superuser"


class InvitationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# Request models
class SendInvitationRequest(BaseModel):
    email: str
    phone: str
    name: str
    role: InvitationRole = InvitationRole.ADMIN


class AcceptInvitationRequest(BaseModel):
    token: str


# Response models
class InvitationUser(BaseModel):
    id: UUID
    email: str
    name: Optional[str] = None
    phone: Optional[str] = None

    class Config:
        populate_by_name = True


class InvitationData(BaseModel):
    id: UUID
    email: str
    name: Optional[str] = None
    role: str
    status: str
    expiresAt: Optional[datetime] = None
    createdAt: Optional[datetime] = None
    invitedByName: Optional[str] = None


class SendInvitationResponse(BaseModel):
    success: bool = True
    message: str = "Invitation sent successfully"
    data: Optional[InvitationData] = None


class AcceptInvitationResponse(BaseModel):
    success: bool = True
    message: str = "Invitation accepted successfully"
    user: Optional[InvitationUser] = None


class PendingInvitationsResponse(BaseModel):
    success: bool = True
    data: List[InvitationData] = []


class CancelInvitationResponse(BaseModel):
    success: bool = True
    message: str = "Invitation cancelled successfully"
