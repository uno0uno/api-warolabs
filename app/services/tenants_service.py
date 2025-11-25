import logging
from datetime import datetime
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.auth import Tenant, UserTenantsResponse
from app.models.tenant import TenantMembersResponse, TenantMemberDetail, TenantMemberProfile

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

async def get_tenant_members(request: Request) -> TenantMembersResponse:
    """
    Get members of the current tenant from session
    """
    try:
        # Get session context from middleware
        session_context = require_valid_session(request)
        current_tenant_id = session_context.tenant_id

        if not current_tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            # Get tenant members with their profile information
            query = """
                SELECT
                    tm.id,
                    tm.tenant_id,
                    tm.user_id,
                    tm.role,
                    p.id as profile_id,
                    p.name,
                    p.user_name,
                    p.email,
                    p.logo_avatar
                FROM tenant_members tm
                INNER JOIN profile p ON tm.user_id = p.id
                WHERE tm.tenant_id = $1
                ORDER BY
                    CASE tm.role
                        WHEN 'superuser' THEN 1
                        WHEN 'admin' THEN 2
                        WHEN 'employee' THEN 3
                        WHEN 'member' THEN 4
                        ELSE 5
                    END,
                    p.name, p.user_name
            """

            member_rows = await conn.fetch(query, current_tenant_id)

            # Convert to TenantMemberDetail models
            members = []
            for row in member_rows:
                profile = TenantMemberProfile(
                    id=row['profile_id'],
                    name=row['name'],
                    user_name=row['user_name'],
                    email=row['email'],
                    logo_avatar=row['logo_avatar']
                )

                member = TenantMemberDetail(
                    id=row['id'],
                    tenant_id=row['tenant_id'],
                    user_id=row['user_id'],
                    role=row['role'],
                    profile=profile
                )
                members.append(member)

            return TenantMembersResponse(data=members)

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching tenant members: {e}", exc_info=True)
        raise AuthenticationError("Error interno del servidor")