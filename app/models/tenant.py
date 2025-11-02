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