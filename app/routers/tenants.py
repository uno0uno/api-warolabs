from fastapi import APIRouter, Request
from app.services.tenants_service import get_user_tenants
from app.models.auth import UserTenantsResponse

router = APIRouter()

@router.get("/user-tenants", response_model=UserTenantsResponse)
async def get_user_tenants_endpoint(request: Request):
    """
    Get tenants associated with the current user
    Requires valid session cookie
    """
    return await get_user_tenants(request)
