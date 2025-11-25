from fastapi import APIRouter, Request
from app.services.tenants_service import get_user_tenants, get_tenant_members
from app.models.auth import UserTenantsResponse
from app.models.tenant import TenantMembersResponse

router = APIRouter()

@router.get("/user-tenants", response_model=UserTenantsResponse)
async def get_user_tenants_endpoint(request: Request):
    """
    Get tenants associated with the current user
    Requires valid session cookie
    """
    return await get_user_tenants(request)

@router.get("/members", response_model=TenantMembersResponse)
async def get_tenant_members_endpoint(request: Request):
    """
    Get members of the current tenant
    Requires valid session cookie with selected tenant
    """
    return await get_tenant_members(request)
