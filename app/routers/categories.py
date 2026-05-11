from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
import asyncpg

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.models.category import (
    CategoriesListResponse,
    Category,
    CategoryCreate,
    CategoryResponse,
)

router = APIRouter()


@router.get("", response_model=CategoriesListResponse, dependencies=[Depends(require_module(Module.MENU))])
async def get_categories_endpoint(
    request: Request,
    search: Optional[str] = Query(None, description="Filter by name (case-insensitive partial match)"),
    limit: int = Query(250, ge=1, le=500),
):
    """
    List categories visible to the current tenant.

    A category is visible when its `tenant_id IS NULL` (global, seeded for every
    tenant) OR when it belongs to the tenant attached to the session.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context is required")

    base_query = """
        SELECT id, name, description, tenant_id, created_at, updated_at
        FROM categories
        WHERE (tenant_id IS NULL OR tenant_id = $1)
    """
    params = [tenant_id]

    if search and search.strip():
        base_query += " AND LOWER(name) LIKE LOWER($2)"
        params.append(f"%{search.strip()}%")

    # Order globals first, then per-tenant alphabetically
    base_query += " ORDER BY name ASC"
    base_query += f" LIMIT ${len(params) + 1}"
    params.append(limit)

    async with get_db_connection() as conn:
        rows = await conn.fetch(base_query, *params)
        categories = [Category(**dict(row)) for row in rows]

    return CategoriesListResponse(
        success=True,
        total=len(categories),
        data=categories,
    )


@router.post("", response_model=CategoryResponse, status_code=201, dependencies=[Depends(require_module(Module.MENU))])
async def create_category_endpoint(request: Request, payload: CategoryCreate):
    """
    Create a category scoped to the current tenant.

    Returns 409 when a category with the same name (case-insensitive) already
    exists either globally OR for this tenant — enforced by the functional
    unique index `idx_categories_name_tenant_unique`.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context is required")

    insert_query = """
        INSERT INTO categories (name, description, tenant_id)
        VALUES ($1, $2, $3)
        RETURNING id, name, description, tenant_id, created_at, updated_at
    """

    try:
        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                insert_query,
                payload.name.strip(),
                (payload.description.strip() if payload.description else None),
                tenant_id,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una categoría con ese nombre",
        )

    return CategoryResponse(success=True, data=Category(**dict(row)))
