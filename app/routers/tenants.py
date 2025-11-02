from fastapi import APIRouter, Request, Response, Depends
from app.services.auth_service import get_session_data
from app.models.tenant import UserTenantsResponse
from app.database import get_db_connection
from app.models.auth import SessionResponse

router = APIRouter()

@router.get("/user-tenants", response_model=UserTenantsResponse)
async def get_user_tenants(request: Request, response: Response):
    """
    Get tenants for current user - uses session cookie authentication
    """
    # First validate session
    session_data = await get_session_data(request, response)
    user_id = session_data.user.id
    
    async with get_db_connection() as conn:
        # Get user's tenants
        tenants_query = """
            SELECT t.id, t.name, t.slug, t.created_at
            FROM tenants t
            JOIN tenant_members tm ON t.id = tm.tenant_id
            WHERE tm.user_id = $1
            ORDER BY t.name
        """
        tenant_results = await conn.fetch(tenants_query, user_id)
        
        tenants = [
            {
                "id": row["id"],
                "name": row["name"],
                "slug": row["slug"],
                "createdAt": row["created_at"]
            }
            for row in tenant_results
        ]
        
        return UserTenantsResponse(tenants=tenants)