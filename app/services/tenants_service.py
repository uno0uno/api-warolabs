import re
import logging
from datetime import datetime
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, AuthorizationError, ValidationError
from app.models.auth import Tenant, UserTenantsResponse
from app.models.tenant import (
    TenantMembersResponse, TenantMemberDetail, TenantMemberProfile,
    DeleteMemberResponse, UpdateMemberRoleResponse, PendingInvitation,
    TenantCreate, TenantCreateResponse, Tenant as TenantModel,
)

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
            # Get tenants where the user has an ACTIVE membership (#201).
            # Terminated members keep their row with the old role; without the
            # is_active filter, the sidebar tenant switcher shows tenants the
            # user can't actually access. See docs/permissions-router-mapping.md §9.
            query = """
                SELECT DISTINCT
                  t.id,
                  t.name,
                  t.slug
                FROM tenants t
                INNER JOIN tenant_members tm ON t.id = tm.tenant_id
                WHERE tm.user_id = $1 AND tm.is_active = true
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
            # Exclude 'customer' role - those are POS clients, not team members
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
                  AND tm.role IN ('superuser', 'admin', 'employee', 'member')
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

            # Get pending invitations for this tenant
            invitations_query = """
                SELECT
                    ti.id, ti.email, ti.role, ti.status, ti.expires_at,
                    p.name as invitee_name,
                    inviter.name as invited_by_name
                FROM tenant_invitations ti
                LEFT JOIN profile p ON ti.user_id = p.id
                LEFT JOIN profile inviter ON ti.invited_by = inviter.id
                WHERE ti.tenant_id = $1 AND ti.status = 'pending'
                ORDER BY ti.expires_at DESC
            """
            invitation_rows = await conn.fetch(invitations_query, current_tenant_id)

            pending_invitations = [
                PendingInvitation(
                    id=row['id'],
                    email=row['email'],
                    name=row['invitee_name'],
                    role=row['role'],
                    status=row['status'],
                    expires_at=row['expires_at'],
                    invited_by_name=row['invited_by_name']
                )
                for row in invitation_rows
            ]

            return TenantMembersResponse(data=members, pending_invitations=pending_invitations)

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching tenant members: {e}", exc_info=True)
        raise AuthenticationError("Error interno del servidor")


async def delete_tenant_member(request: Request, member_id: str) -> DeleteMemberResponse:
    """
    Remove a member from the current tenant (does not delete their profile)
    Only admin or superuser can remove members
    Cannot remove yourself
    """
    try:
        session_context = require_valid_session(request)
        current_user_id = session_context.user_id
        current_tenant_id = session_context.tenant_id

        if not current_tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            # Check current user's role
            user_role = await conn.fetchval(
                "SELECT role FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                current_user_id, current_tenant_id
            )

            if user_role not in ('admin', 'superuser'):
                raise AuthorizationError("Solo admin o superuser pueden eliminar miembros")

            # Get member to delete info
            member_info = await conn.fetchrow(
                """SELECT tm.id, tm.user_id, p.name, p.email
                   FROM tenant_members tm
                   JOIN profile p ON tm.user_id = p.id
                   WHERE tm.id = $1 AND tm.tenant_id = $2""",
                member_id, current_tenant_id
            )

            if not member_info:
                raise ValidationError("Miembro no encontrado")

            # Cannot delete yourself
            if str(member_info['user_id']) == str(current_user_id):
                raise ValidationError("No puedes eliminarte a ti mismo del equipo")

            # Delete from tenant_members only (not from profile)
            await conn.execute(
                "DELETE FROM tenant_members WHERE id = $1 AND tenant_id = $2",
                member_id, current_tenant_id
            )

            member_name = member_info['name'] or member_info['email']
            logger.info(f"🗑️ Member {member_name} removed from tenant {current_tenant_id}")

            return DeleteMemberResponse(
                success=True,
                message=f"{member_name} ha sido eliminado del equipo"
            )

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error deleting tenant member: {e}", exc_info=True)
        raise ValidationError("Error al eliminar miembro")


async def update_member_role(request: Request, member_id: str, new_role: str) -> UpdateMemberRoleResponse:
    """
    Update a member's role in the current tenant
    Only superuser can change roles
    Cannot change your own role
    """
    VALID_ROLES = ['superuser', 'admin', 'employee', 'member']

    try:
        session_context = require_valid_session(request)
        current_user_id = session_context.user_id
        current_tenant_id = session_context.tenant_id

        if not current_tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        # Validate role
        if new_role not in VALID_ROLES:
            raise ValidationError(f"Rol inválido. Roles válidos: {', '.join(VALID_ROLES)}")

        async with get_db_connection() as conn:
            # Check current user's role - ONLY superuser can change roles
            user_role = await conn.fetchval(
                "SELECT role FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                current_user_id, current_tenant_id
            )

            if user_role != 'superuser':
                raise AuthorizationError("Solo el super usuario puede cambiar roles")

            # Get member to update info
            member_info = await conn.fetchrow(
                """SELECT tm.id, tm.user_id, tm.role, p.name, p.email
                   FROM tenant_members tm
                   JOIN profile p ON tm.user_id = p.id
                   WHERE tm.id = $1 AND tm.tenant_id = $2""",
                member_id, current_tenant_id
            )

            if not member_info:
                raise ValidationError("Miembro no encontrado")

            # Cannot change your own role
            if str(member_info['user_id']) == str(current_user_id):
                raise ValidationError("No puedes cambiar tu propio rol")

            # Check if role is actually changing
            if member_info['role'] == new_role:
                raise ValidationError(f"El miembro ya tiene el rol '{new_role}'")

            # Update role
            await conn.execute(
                "UPDATE tenant_members SET role = $1 WHERE id = $2 AND tenant_id = $3",
                new_role, member_id, current_tenant_id
            )

            # Fetch updated member
            updated_row = await conn.fetchrow(
                """SELECT
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
                WHERE tm.id = $1 AND tm.tenant_id = $2""",
                member_id, current_tenant_id
            )

            profile = TenantMemberProfile(
                id=updated_row['profile_id'],
                name=updated_row['name'],
                user_name=updated_row['user_name'],
                email=updated_row['email'],
                logo_avatar=updated_row['logo_avatar']
            )

            updated_member = TenantMemberDetail(
                id=updated_row['id'],
                tenant_id=updated_row['tenant_id'],
                user_id=updated_row['user_id'],
                role=updated_row['role'],
                profile=profile
            )

            member_name = member_info['name'] or member_info['email']
            old_role = member_info['role']
            logger.info(f"🔄 Role updated for {member_name}: {old_role} → {new_role}")

            return UpdateMemberRoleResponse(
                success=True,
                message=f"Rol de {member_name} actualizado a {new_role}",
                data=updated_member
            )

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error updating member role: {e}", exc_info=True)
        raise ValidationError("Error al actualizar rol")


def _generate_slug(name: str) -> str:
    """Convert a tenant name into a URL-safe slug. Handles Spanish characters."""
    slug = name.lower()
    slug = re.sub(r'[áàäâã]', 'a', slug)
    slug = re.sub(r'[éèëê]', 'e', slug)
    slug = re.sub(r'[íìïî]', 'i', slug)
    slug = re.sub(r'[óòöôõ]', 'o', slug)
    slug = re.sub(r'[úùüû]', 'u', slug)
    slug = re.sub(r'ñ', 'n', slug)
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    return slug.strip('-')


async def create_tenant(request: Request, body: TenantCreate) -> TenantCreateResponse:
    """
    Create a new tenant for the authenticated user.

    Creates tenant + tenant_public_profiles + adds creator as superuser
    + seeds PUC colombiano accounts (all active account_templates via seed_tenant_accounts()).
    All in a single transaction.
    """
    try:
        session_context = require_valid_session(request)
        user_id = session_context.user_id
        if not user_id:
            raise AuthenticationError("Sesión no válida")

        base_slug = _generate_slug(body.name)

        async with get_db_connection() as conn:
            # Ensure slug uniqueness — append counter if taken
            slug = base_slug
            counter = 1
            while await conn.fetchval("SELECT 1 FROM tenants WHERE slug = $1", slug):
                slug = f"{base_slug}-{counter}"
                counter += 1

            async with conn.transaction():
                # 1. Create tenant
                tenant_row = await conn.fetchrow(
                    """INSERT INTO tenants (name, slug, created_at)
                       VALUES ($1, $2, NOW())
                       RETURNING id, name, slug, created_at""",
                    body.name, slug
                )
                tenant_id = tenant_row['id']

                # 2. Create minimal public profile
                await conn.execute(
                    """INSERT INTO tenant_public_profiles
                           (tenant_id, display_name, slug,
                            is_active, is_manually_open, welcome_email_sent, tables_enabled,
                            comandas_enabled, kds_enabled)
                       VALUES ($1, $2, $3, true, false, false, false, false, false)""",
                    tenant_id, body.name, slug
                )

                # 3. Add creator as superuser
                await conn.execute(
                    """INSERT INTO tenant_members (id, tenant_id, user_id, role)
                       VALUES (gen_random_uuid(), $1, $2, 'superuser')""",
                    tenant_id, user_id
                )

                # 4. Seed PUC colombiano accounts (all active account_templates)
                await conn.execute("SELECT seed_tenant_accounts($1)", tenant_id)

            logger.info(f"✅ Tenant created: {body.name} ({slug}) by user {user_id}")

            return TenantCreateResponse(
                data=TenantModel(
                    id=tenant_row['id'],
                    name=tenant_row['name'],
                    slug=tenant_row['slug'],
                    created_at=tenant_row['created_at'],
                )
            )

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error creating tenant: {e}", exc_info=True)
        raise