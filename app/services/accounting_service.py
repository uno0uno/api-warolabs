import logging
from typing import Optional, List
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, AuthorizationError, ValidationError
from app.models.accounting import (
    TenantAccount,
    TenantAccountCreate,
    TenantAccountUpdate,
    TenantAccountResponse,
    TenantAccountsListResponse,
)

logger = logging.getLogger(__name__)

VALID_ACCOUNT_TYPES = ['asset', 'liability', 'equity', 'income', 'expense', 'cogs']
VALID_NORMAL_BALANCES = ['debit', 'credit']


def _derive_level(code: str) -> int:
    """Derive PUC level from code length: 1→class, 2→group, 4→account, 6→sub-account."""
    length = len(code.strip())
    if length == 1:
        return 1
    if length == 2:
        return 2
    if length <= 4:
        return 4
    return 6


async def get_accounts(
    request: Request,
    account_class: Optional[str] = None,
    account_type: Optional[str] = None,
    active: Optional[bool] = None,
) -> TenantAccountsListResponse:
    """
    Return the full chart of accounts for the authenticated tenant.
    Optional filters: class (PUC class digit), type, active status.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        conditions = ["ta.tenant_id = $1"]
        params: list = [tenant_id]

        if account_class is not None:
            params.append(account_class)
            conditions.append(f"ta.account_class = ${len(params)}")

        if account_type is not None:
            params.append(account_type)
            conditions.append(f"ta.account_type = ${len(params)}")

        if active is not None:
            params.append(active)
            conditions.append(f"ta.is_active = ${len(params)}")

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                ta.id,
                ta.tenant_id,
                ta.template_id,
                ta.code,
                ta.name,
                ta.account_class,
                ta.account_type,
                ta.normal_balance,
                ta.level,
                ta.parent_id,
                ta.is_detail,
                ta.is_system,
                ta.is_active,
                ta.created_at
            FROM tenant_accounts ta
            WHERE {where_clause}
            ORDER BY ta.code
        """

        async with get_db_connection() as conn:
            rows = await conn.fetch(query, *params)

        accounts = [
            TenantAccount(
                id=row['id'],
                tenant_id=row['tenant_id'],
                template_id=row['template_id'],
                code=row['code'],
                name=row['name'],
                account_class=row['account_class'],
                account_type=row['account_type'],
                normal_balance=row['normal_balance'],
                level=row['level'],
                parent_id=row['parent_id'],
                is_detail=row['is_detail'],
                is_system=row['is_system'],
                is_active=row['is_active'],
                created_at=row['created_at'],
            )
            for row in rows
        ]

        return TenantAccountsListResponse(data=accounts)

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching accounts: {e}", exc_info=True)
        raise ValidationError("Error al obtener cuentas")


async def create_account(request: Request, body: TenantAccountCreate) -> TenantAccountResponse:
    """
    Create a custom account for the tenant.
    Code must be unique per tenant. is_system is always false for API-created accounts.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if body.account_type not in VALID_ACCOUNT_TYPES:
            raise ValidationError(f"Tipo de cuenta inválido. Válidos: {', '.join(VALID_ACCOUNT_TYPES)}")

        if body.normal_balance not in VALID_NORMAL_BALANCES:
            raise ValidationError(f"Saldo normal inválido. Válidos: {', '.join(VALID_NORMAL_BALANCES)}")

        code = body.code.strip()
        if not code:
            raise ValidationError("El código de la cuenta es requerido")

        level = _derive_level(code)

        async with get_db_connection() as conn:
            # Ensure code is unique within tenant
            existing = await conn.fetchval(
                "SELECT 1 FROM tenant_accounts WHERE tenant_id = $1 AND code = $2",
                tenant_id, code
            )
            if existing:
                raise ValidationError(f"Ya existe una cuenta con el código '{code}'")

            # Validate parent_id belongs to same tenant
            parent_id = body.parent_id
            if parent_id is not None:
                parent_exists = await conn.fetchval(
                    "SELECT 1 FROM tenant_accounts WHERE id = $1 AND tenant_id = $2",
                    parent_id, tenant_id
                )
                if not parent_exists:
                    raise ValidationError("La cuenta padre no existe o pertenece a otro tenant")

            row = await conn.fetchrow(
                """INSERT INTO tenant_accounts
                       (tenant_id, template_id, code, name, account_class, account_type,
                        normal_balance, level, parent_id, is_detail, is_system, is_active)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, false, true)
                   RETURNING id, tenant_id, template_id, code, name, account_class,
                             account_type, normal_balance, level, parent_id,
                             is_detail, is_system, is_active, created_at""",
                tenant_id,
                body.template_id,
                code,
                body.name,
                body.account_class,
                body.account_type,
                body.normal_balance,
                level,
                parent_id,
                body.is_detail,
            )

        account = TenantAccount(
            id=row['id'],
            tenant_id=row['tenant_id'],
            template_id=row['template_id'],
            code=row['code'],
            name=row['name'],
            account_class=row['account_class'],
            account_type=row['account_type'],
            normal_balance=row['normal_balance'],
            level=row['level'],
            parent_id=row['parent_id'],
            is_detail=row['is_detail'],
            is_system=row['is_system'],
            is_active=row['is_active'],
            created_at=row['created_at'],
        )

        logger.info(f"✅ Account created: {code} for tenant {tenant_id}")
        return TenantAccountResponse(data=account)

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error creating account: {e}", exc_info=True)
        raise ValidationError("Error al crear cuenta")


async def update_account(
    request: Request, account_id: UUID, body: TenantAccountUpdate
) -> TenantAccountResponse:
    """
    Update name, active status, or is_detail flag.
    System accounts can be deactivated but not renamed.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            current = await conn.fetchrow(
                """SELECT id, is_system, is_active, name, is_detail
                   FROM tenant_accounts WHERE id = $1 AND tenant_id = $2""",
                account_id, tenant_id
            )
            if not current:
                raise ValidationError("Cuenta no encontrada")

            # System accounts cannot be renamed
            if current['is_system'] and body.name is not None and body.name != current['name']:
                raise AuthorizationError("No se puede renombrar una cuenta del sistema")

            # Build SET clause dynamically
            updates: list = []
            params: list = []

            if body.name is not None:
                params.append(body.name)
                updates.append(f"name = ${len(params)}")

            if body.is_active is not None:
                params.append(body.is_active)
                updates.append(f"is_active = ${len(params)}")

            if body.is_detail is not None:
                params.append(body.is_detail)
                updates.append(f"is_detail = ${len(params)}")

            if not updates:
                raise ValidationError("No hay campos para actualizar")

            params.append(account_id)
            params.append(tenant_id)
            set_clause = ", ".join(updates)

            row = await conn.fetchrow(
                f"""UPDATE tenant_accounts SET {set_clause}
                    WHERE id = ${len(params) - 1} AND tenant_id = ${len(params)}
                    RETURNING id, tenant_id, template_id, code, name, account_class,
                              account_type, normal_balance, level, parent_id,
                              is_detail, is_system, is_active, created_at""",
                *params
            )

        account = TenantAccount(
            id=row['id'],
            tenant_id=row['tenant_id'],
            template_id=row['template_id'],
            code=row['code'],
            name=row['name'],
            account_class=row['account_class'],
            account_type=row['account_type'],
            normal_balance=row['normal_balance'],
            level=row['level'],
            parent_id=row['parent_id'],
            is_detail=row['is_detail'],
            is_system=row['is_system'],
            is_active=row['is_active'],
            created_at=row['created_at'],
        )

        logger.info(f"✏️ Account updated: {account_id} for tenant {tenant_id}")
        return TenantAccountResponse(data=account)

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error updating account: {e}", exc_info=True)
        raise ValidationError("Error al actualizar cuenta")


async def delete_account(request: Request, account_id: UUID) -> dict:
    """
    Soft-delete (deactivate) a custom account.
    System accounts and accounts with journal lines cannot be deleted.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            account = await conn.fetchrow(
                "SELECT id, code, name, is_system FROM tenant_accounts WHERE id = $1 AND tenant_id = $2",
                account_id, tenant_id
            )
            if not account:
                raise ValidationError("Cuenta no encontrada")

            if account['is_system']:
                raise AuthorizationError("No se puede eliminar una cuenta del sistema")

            # Guard: block if account has journal lines
            has_lines = await conn.fetchval(
                "SELECT 1 FROM tenant_journal_lines WHERE account_id = $1 LIMIT 1",
                account_id
            )
            if has_lines:
                raise ValidationError(
                    "No se puede eliminar una cuenta con movimientos contables. Use desactivar."
                )

            await conn.execute(
                "UPDATE tenant_accounts SET is_active = false WHERE id = $1 AND tenant_id = $2",
                account_id, tenant_id
            )

        logger.info(f"🗑️ Account soft-deleted: {account['code']} for tenant {tenant_id}")
        return {"success": True, "message": f"Cuenta '{account['name']}' desactivada"}

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error deleting account: {e}", exc_info=True)
        raise ValidationError("Error al eliminar cuenta")
