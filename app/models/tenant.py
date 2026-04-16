from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
from uuid import UUID

class Tenant(BaseModel):
    id: UUID
    name: str
    slug: str
    created_at: datetime = Field(alias='createdAt')
    
    class Config:
        populate_by_name = True

class TenantMember(BaseModel):
    user_id: UUID = Field(alias='userId')
    tenant_id: UUID = Field(alias='tenantId')
    role: str
    joined_at: datetime = Field(alias='joinedAt')
    
    class Config:
        populate_by_name = True

class UserTenantsResponse(BaseModel):
    success: bool = True
    tenants: List[Tenant]

class TenantMemberProfile(BaseModel):
    id: UUID
    name: Optional[str] = None
    user_name: Optional[str] = None
    email: str
    logo_avatar: Optional[str] = None

class TenantMemberDetail(BaseModel):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    user_id: UUID = Field(alias='userId')
    role: str
    profile: TenantMemberProfile

    class Config:
        populate_by_name = True

class PendingInvitation(BaseModel):
    id: UUID
    email: str
    name: Optional[str] = None
    role: str
    status: str
    expires_at: datetime = Field(alias='expiresAt')
    invited_by_name: Optional[str] = Field(None, alias='invitedByName')

    class Config:
        populate_by_name = True


class TenantMembersResponse(BaseModel):
    success: bool = True
    data: List[TenantMemberDetail]
    pending_invitations: List[PendingInvitation] = Field(default=[], alias='pendingInvitations')

    class Config:
        populate_by_name = True

class DeleteMemberResponse(BaseModel):
    success: bool = True
    message: str


class UpdateMemberRoleRequest(BaseModel):
    role: str = Field(..., description="New role: superuser, admin, employee, member")


class UpdateMemberRoleResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[TenantMemberDetail] = None


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class TenantCreateResponse(BaseModel):
    success: bool = True
    data: Tenant