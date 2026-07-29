import secrets
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from uuid import UUID
from fastapi import Request, HTTPException

from app.database import get_db_connection
from app.core.security import get_session_from_request
from app.models.api_token import (
    ApiTokenCreate,
    ApiToken,
    ApiTokenWithSecret,
    AVAILABLE_SCOPES
)
from app.services.billing_service import check_plan_quota_growth

logger = logging.getLogger(__name__)

# Prefijo de marca para las API keys
API_KEY_PREFIX = "waro_"


def to_uuid(value) -> UUID:
    """Convierte un valor a UUID de Python (maneja strings y UUIDs de asyncpg)"""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def generate_api_key() -> tuple[str, str, str]:
    """
    Genera una API key segura con formato: waro_<random_string>

    Returns:
        tuple: (full_key, key_prefix, key_hash)
        - full_key: La key completa para dar al usuario (ej: waro_sk_a1b2c3d4e5f6...)
        - key_prefix: Los primeros caracteres para identificacion (ej: waro_sk_a1b2)
        - key_hash: SHA-256 hash para almacenar en BD
    """
    # Generar 32 bytes de datos aleatorios (256 bits de entropia)
    random_bytes = secrets.token_bytes(32)
    random_string = secrets.token_urlsafe(32)

    # Formato: waro_sk_<random_string> (sk = secret key)
    full_key = f"{API_KEY_PREFIX}sk_{random_string}"

    # Prefijo para identificacion: primeros 12 caracteres despues del prefijo
    key_prefix = full_key[:16]  # waro_sk_xxxxxxxx

    # Hash SHA-256 del token completo
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()

    return full_key, key_prefix, key_hash


def hash_api_key(api_key: str) -> str:
    """Hash una API key para busqueda en BD"""
    return hashlib.sha256(api_key.encode()).hexdigest()


def validate_scopes(scopes: List[str]) -> List[str]:
    """Valida y filtra scopes invalidos"""
    valid_scopes = [s for s in scopes if s in AVAILABLE_SCOPES]
    if not valid_scopes:
        valid_scopes = ["read"]  # Default scope
    return valid_scopes


async def create_api_token(request: Request, token_data: ApiTokenCreate) -> dict:
    """
    Crea un nuevo API token para el tenant actual
    """
    # Obtener sesion del usuario
    session = await get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="No valid session found")

    user_id = session.get('user_id')
    tenant_id = session.get('tenant_id')

    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant selected")

    # Validar scopes
    valid_scopes = validate_scopes(token_data.scopes)

    # Calcular fecha de expiracion
    expires_at = None
    if token_data.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=token_data.expires_in_days)

    # Generar API key
    full_key, key_prefix, key_hash = generate_api_key()

    async with get_db_connection() as conn:
        # Verificar que el usuario tiene permisos (admin o superuser)
        role_check = await conn.fetchrow("""
            SELECT role FROM tenant_members
            WHERE tenant_id = $1 AND user_id = $2
        """, to_uuid(tenant_id), to_uuid(user_id))

        if not role_check or role_check['role'] not in ('admin', 'superuser'):
            raise HTTPException(
                status_code=403,
                detail="Only admin or superuser can create API tokens"
            )

        await check_plan_quota_growth(conn, to_uuid(tenant_id), "api_tokens")

        # Insertar el token
        result = await conn.fetchrow("""
            INSERT INTO api_tokens (
                tenant_id, created_by, name, key_prefix, key_hash,
                scopes, expires_at, is_active
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
            RETURNING id, created_at
        """,
            to_uuid(tenant_id),
            to_uuid(user_id),
            token_data.name,
            key_prefix,
            key_hash,
            valid_scopes,
            expires_at
        )

        logger.info(f"API token created: {key_prefix}... for tenant {tenant_id}")

        return {
            "success": True,
            "message": "API token created successfully. Save the secret key - it won't be shown again!",
            "data": {
                "id": str(result['id']),
                "name": token_data.name,
                "keyPrefix": key_prefix,
                "secretKey": full_key,  # Solo se muestra esta vez
                "scopes": valid_scopes,
                "expiresAt": expires_at.isoformat() if expires_at else None,
                "createdAt": result['created_at'].isoformat()
            }
        }


async def list_api_tokens(request: Request) -> dict:
    """
    Lista todos los API tokens del tenant actual
    """
    session = await get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="No valid session found")

    tenant_id = session.get('tenant_id')
    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant selected")

    async with get_db_connection(use_transaction=False) as conn:
        tokens = await conn.fetch("""
            SELECT
                t.id, t.name, t.key_prefix, t.scopes,
                t.expires_at, t.last_used_at, t.created_at, t.is_active,
                p.name as created_by_name
            FROM api_tokens t
            LEFT JOIN profile p ON t.created_by = p.id
            WHERE t.tenant_id = $1
            ORDER BY t.created_at DESC
        """, to_uuid(tenant_id))

        return {
            "success": True,
            "data": [
                {
                    "id": str(row['id']),
                    "name": row['name'],
                    "keyPrefix": row['key_prefix'],
                    "scopes": row['scopes'],
                    "expiresAt": row['expires_at'].isoformat() if row['expires_at'] else None,
                    "lastUsedAt": row['last_used_at'].isoformat() if row['last_used_at'] else None,
                    "createdAt": row['created_at'].isoformat(),
                    "isActive": row['is_active'],
                    "createdByName": row['created_by_name']
                }
                for row in tokens
            ]
        }


async def revoke_api_token(request: Request, token_id: str) -> dict:
    """
    Revoca (desactiva) un API token
    """
    session = await get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="No valid session found")

    user_id = session.get('user_id')
    tenant_id = session.get('tenant_id')

    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant selected")

    async with get_db_connection() as conn:
        # Verificar permisos
        role_check = await conn.fetchrow("""
            SELECT role FROM tenant_members
            WHERE tenant_id = $1 AND user_id = $2
        """, to_uuid(tenant_id), to_uuid(user_id))

        if not role_check or role_check['role'] not in ('admin', 'superuser'):
            raise HTTPException(
                status_code=403,
                detail="Only admin or superuser can revoke API tokens"
            )

        # Revocar el token (verificando que pertenece al tenant)
        result = await conn.execute("""
            UPDATE api_tokens
            SET is_active = FALSE
            WHERE id = $1 AND tenant_id = $2
        """, to_uuid(token_id), to_uuid(tenant_id))

        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Token not found")

        logger.info(f"API token revoked: {token_id} by user {user_id}")

        return {
            "success": True,
            "message": "API token revoked successfully"
        }


async def delete_api_token(request: Request, token_id: str) -> dict:
    """
    Elimina permanentemente un API token
    """
    session = await get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="No valid session found")

    user_id = session.get('user_id')
    tenant_id = session.get('tenant_id')

    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant selected")

    async with get_db_connection() as conn:
        # Verificar permisos
        role_check = await conn.fetchrow("""
            SELECT role FROM tenant_members
            WHERE tenant_id = $1 AND user_id = $2
        """, to_uuid(tenant_id), to_uuid(user_id))

        if not role_check or role_check['role'] not in ('admin', 'superuser'):
            raise HTTPException(
                status_code=403,
                detail="Only admin or superuser can delete API tokens"
            )

        # Eliminar el token
        result = await conn.execute("""
            DELETE FROM api_tokens
            WHERE id = $1 AND tenant_id = $2
        """, to_uuid(token_id), to_uuid(tenant_id))

        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Token not found")

        logger.info(f"API token deleted: {token_id} by user {user_id}")

        return {
            "success": True,
            "message": "API token deleted permanently"
        }


async def validate_api_key(api_key: str) -> Optional[dict]:
    """
    Valida una API key y retorna la informacion del tenant/scopes
    Usado por el middleware de autenticacion

    Returns:
        dict con tenant_id, scopes, token_id si es valido
        None si no es valido
    """
    if not api_key or not api_key.startswith(API_KEY_PREFIX):
        return None

    key_hash = hash_api_key(api_key)

    async with get_db_connection() as conn:
        token = await conn.fetchrow("""
            SELECT id, tenant_id, scopes, expires_at, is_active
            FROM api_tokens
            WHERE key_hash = $1
        """, key_hash)

        if not token:
            return None

        # Verificar si esta activo
        if not token['is_active']:
            logger.warning(f"Attempted use of revoked API token: {api_key[:16]}...")
            return None

        # Verificar expiracion
        if token['expires_at'] and token['expires_at'] < datetime.now(timezone.utc):
            logger.warning(f"Attempted use of expired API token: {api_key[:16]}...")
            return None

        # Actualizar last_used_at
        await conn.execute("""
            UPDATE api_tokens SET last_used_at = NOW() WHERE id = $1
        """, token['id'])

        return {
            "token_id": str(token['id']),
            "tenant_id": str(token['tenant_id']),
            "scopes": token['scopes']
        }


async def update_api_token(request: Request, token_id: str, name: Optional[str] = None,
                           scopes: Optional[List[str]] = None, is_active: Optional[bool] = None) -> dict:
    """
    Actualiza un API token (nombre, scopes, estado)
    """
    session = await get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="No valid session found")

    user_id = session.get('user_id')
    tenant_id = session.get('tenant_id')

    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant selected")

    async with get_db_connection() as conn:
        # Verificar permisos
        role_check = await conn.fetchrow("""
            SELECT role FROM tenant_members
            WHERE tenant_id = $1 AND user_id = $2
        """, to_uuid(tenant_id), to_uuid(user_id))

        if not role_check or role_check['role'] not in ('admin', 'superuser'):
            raise HTTPException(
                status_code=403,
                detail="Only admin or superuser can update API tokens"
            )

        existing = await conn.fetchrow(
            """
            SELECT is_active
            FROM api_tokens
            WHERE id = $1 AND tenant_id = $2
            """,
            to_uuid(token_id),
            to_uuid(tenant_id),
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Token not found")

        # Construir query dinamica
        updates = []
        params = [to_uuid(token_id), to_uuid(tenant_id)]
        param_idx = 3

        if name is not None:
            updates.append(f"name = ${param_idx}")
            params.append(name)
            param_idx += 1

        if scopes is not None:
            valid_scopes = validate_scopes(scopes)
            updates.append(f"scopes = ${param_idx}")
            params.append(valid_scopes)
            param_idx += 1

        if is_active is not None:
            if is_active is True and not existing["is_active"]:
                await check_plan_quota_growth(conn, to_uuid(tenant_id), "api_tokens")
            updates.append(f"is_active = ${param_idx}")
            params.append(is_active)
            param_idx += 1

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        query = f"""
            UPDATE api_tokens
            SET {', '.join(updates)}
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, name, key_prefix, scopes, expires_at, last_used_at, created_at, is_active
        """

        result = await conn.fetchrow(query, *params)

        if not result:
            raise HTTPException(status_code=404, detail="Token not found")

        # Obtener nombre del creador
        creator = await conn.fetchrow("""
            SELECT p.name FROM api_tokens t
            JOIN profile p ON t.created_by = p.id
            WHERE t.id = $1
        """, to_uuid(token_id))

        logger.info(f"API token updated: {token_id} by user {user_id}")

        return {
            "success": True,
            "message": "API token updated successfully",
            "data": {
                "id": str(result['id']),
                "name": result['name'],
                "keyPrefix": result['key_prefix'],
                "scopes": result['scopes'],
                "expiresAt": result['expires_at'].isoformat() if result['expires_at'] else None,
                "lastUsedAt": result['last_used_at'].isoformat() if result['last_used_at'] else None,
                "createdAt": result['created_at'].isoformat(),
                "isActive": result['is_active'],
                "createdByName": creator['name'] if creator else None
            }
        }
