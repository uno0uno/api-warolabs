"""
Menu Router - Generic menu endpoints
"""
from fastapi import APIRouter, Request, Query
from typing import Literal
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError

router = APIRouter()

ENTITY_TABLE_MAP = {
    "recipe-bases": "product_base_types",
    "modifier-groups": "modifier_groups",
    "products": "product",
}


@router.get("/check-name")
async def check_name_availability(
    request: Request,
    entity: Literal["recipe-bases", "modifier-groups", "products"] = Query(...),
    name: str = Query(..., min_length=1),
):
    """
    Check if a name is available for a given menu entity within the current tenant.

    Returns { available: true } if the name is free, { available: false } if taken.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    table = ENTITY_TABLE_MAP[entity]

    try:
        async with get_db_connection() as conn:
            exists = await conn.fetchval(
                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE LOWER(name) = LOWER($1) AND tenant_id = $2)",
                name.strip(),
                tenant_id,
            )
        return {"available": not exists}
    except Exception as e:
        raise APIError(f"Error checking name availability: {str(e)}")
