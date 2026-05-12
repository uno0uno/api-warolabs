from fastapi import APIRouter, Depends, Request
from app.core.permissions import Module, require_module
from app.services.tenants_service import get_user_tenants, get_tenant_members, delete_tenant_member, update_member_role, create_tenant
from app.models.auth import UserTenantsResponse
from app.models.tenant import TenantMembersResponse, DeleteMemberResponse, UpdateMemberRoleRequest, UpdateMemberRoleResponse, TenantCreate, TenantCreateResponse

router = APIRouter()

# NOTE: NOT gated under EQUIPO — this is the onboarding / add-new-tenant flow.
# Callers may have no current tenant (role=null) or non-owner roles wanting to
# start their own tenant. Gating would block legitimate self-service signup.
# The service makes the caller superuser of the newly created tenant.
@router.post("", response_model=TenantCreateResponse)
async def create_tenant_endpoint(request: Request, body: TenantCreate):
    """
    Create a new tenant for the authenticated user.
    The caller becomes superuser and receives the 52 PUC colombiano accounts automatically.
    Requires valid session cookie.
    """
    return await create_tenant(request, body)


# NOTE: NOT gated under EQUIPO — this is the sidebar tenant-switcher endpoint
# called by every authenticated user regardless of role. Frontend consumer:
# stores/tenants.ts:59 (populates DashboardTenantSelector.vue for cashier
# through owner). Gating under owner-only EQUIPO would break the switcher
# for all non-owner roles with multiple tenant memberships.
@router.get("/user-tenants", response_model=UserTenantsResponse)
async def get_user_tenants_endpoint(request: Request):
    """
    Get tenants associated with the current user
    Requires valid session cookie
    """
    return await get_user_tenants(request)

@router.get("/members", response_model=TenantMembersResponse, dependencies=[Depends(require_module(Module.EQUIPO))])
async def get_tenant_members_endpoint(request: Request):
    """
    Get members of the current tenant
    Requires valid session cookie with selected tenant
    """
    return await get_tenant_members(request)

@router.delete("/members/{member_id}", response_model=DeleteMemberResponse, dependencies=[Depends(require_module(Module.EQUIPO))])
async def delete_tenant_member_endpoint(request: Request, member_id: str):
    """
    Remove a member from the current tenant
    Only removes from tenant_members, does not delete profile
    Requires admin or superuser role
    """
    return await delete_tenant_member(request, member_id)


@router.put("/members/{member_id}/role", response_model=UpdateMemberRoleResponse, dependencies=[Depends(require_module(Module.EQUIPO))])
async def update_member_role_endpoint(request: Request, member_id: str, body: UpdateMemberRoleRequest):
    """
    Update a member's role in the current tenant
    Only superuser can change roles
    Cannot change your own role
    Valid roles: superuser, admin, employee, member
    """
    return await update_member_role(request, member_id, body.role)
