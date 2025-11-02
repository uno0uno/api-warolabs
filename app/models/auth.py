from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID

class User(BaseModel):
    id: UUID
    email: str
    name: Optional[str] = None
    created_at: datetime = Field(alias='createdAt')
    
    class Config:
        populate_by_name = True

class Session(BaseModel):
    expires_at: datetime = Field(alias='expiresAt')
    created_at: datetime = Field(alias='createdAt')
    last_activity_at: Optional[datetime] = Field(alias='lastActivity', default=None)
    ip_address: Optional[str] = Field(alias='ipAddress', default=None)
    login_method: Optional[str] = Field(alias='loginMethod', default=None)
    tenant_id: Optional[UUID] = Field(alias='tenantId', default=None)
    
    class Config:
        populate_by_name = True

class Tenant(BaseModel):
    id: UUID
    name: str
    slug: str

class SessionResponse(BaseModel):
    success: bool = True
    user: User
    session: Session
    current_tenant: Optional[Tenant] = Field(alias='currentTenant', default=None)
    
    class Config:
        populate_by_name = True