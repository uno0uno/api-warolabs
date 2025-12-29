from fastapi import APIRouter, Request
from app.services.tenants_service import get_user_tenants, get_tenant_members, delete_tenant_member
from app.models.auth import UserTenantsResponse
from app.models.tenant import TenantMembersResponse, DeleteMemberResponse

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

@router.delete("/members/{member_id}", response_model=DeleteMemberResponse)
async def delete_tenant_member_endpoint(request: Request, member_id: str):
    """
    Remove a member from the current tenant
    Only removes from tenant_members, does not delete profile
    Requires admin or superuser role
    """
    return await delete_tenant_member(request, member_id)
