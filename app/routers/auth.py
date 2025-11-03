from fastapi import APIRouter, Request, Response
from app.services.auth_service import get_session_data, switch_tenant
from app.services.magic_link_service import send_magic_link, verify_code, verify_token
from app.models.auth import (
    SessionResponse, 
    MagicLinkRequest, 
    MagicLinkResponse,
    VerifyCodeRequest,
    VerifyCodeResponse,
    VerifyTokenRequest,
    VerifyTokenResponse,
    SwitchTenantRequest,
    SwitchTenantResponse
)

router = APIRouter()

@router.get("/session", response_model=SessionResponse)
async def get_session(request: Request, response: Response):
    """
    Get current session data
    """
    return await get_session_data(request, response)

@router.post("/sign-in-magic-link", response_model=MagicLinkResponse)
async def sign_in_magic_link(request: Request, payload: MagicLinkRequest):
    """
    Send magic link for authentication
    Tenant context automatically detected from request origin via middleware
    """
    return await send_magic_link(request, payload.email, payload.redirect)

@router.post("/verify-code", response_model=VerifyCodeResponse)
async def verify_magic_code(request: Request, response: Response, payload: VerifyCodeRequest):
    """
    Verify magic link code and create session
    Tenant context automatically validated from request origin via middleware
    """
    return await verify_code(request, response, payload.email, payload.code)

@router.post("/verify", response_model=VerifyTokenResponse)
async def verify_magic_token(request: Request, response: Response, payload: VerifyTokenRequest):
    """
    Verify magic link token and create session
    Tenant context automatically validated from request origin via middleware
    """
    return await verify_token(request, response, payload.email, payload.token)

@router.post("/switch-tenant", response_model=SwitchTenantResponse)
async def switch_tenant_endpoint(request: Request, response: Response, payload: SwitchTenantRequest):
    """
    Switch to a different tenant for the current user
    Requires valid session cookie and user must be member of target tenant
    """
    return await switch_tenant(request, response, payload.tenantSlug)

@router.post("/signout")
async def signout_placeholder():
    return {"message": "Auth signout endpoint - coming soon"}