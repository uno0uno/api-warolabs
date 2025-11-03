import logging
from datetime import datetime
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.auth import Tenant, UserTenantsResponse

logger = logging.getLogger(__name__)

async def get_user_tenants(request: Request) -> UserTenantsResponse:
    """
    Get tenants associated with the current user from session
    """
    try:
        # Get session context from middleware
        session_context = require_valid_session(request)
        user_id = session_context.user_id
        
        
        async with get_db_connection() as conn:
            # Get tenants for the user
            query = """
                SELECT DISTINCT 
                  t.id,
                  t.name,
                  t.slug
                FROM tenants t
                INNER JOIN tenant_members tm ON t.id = tm.tenant_id
                WHERE tm.user_id = $1
                ORDER BY t.name
            """
            
            tenant_rows = await conn.fetch(query, user_id)
            
            
            # Convert to Tenant models
            tenants = []
            for row in tenant_rows:
                tenant = Tenant(
                    id=row['id'],
                    name=row['name'],
                    slug=row['slug']
                )
                tenants.append(tenant)
            
            return UserTenantsResponse(
                data=tenants,
                timestamp=datetime.utcnow().isoformat()
            )
            
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching user tenants: {e}", exc_info=True)
        raise AuthenticationError("Error interno del servidor")