from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
import asyncpg

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.models.category import (
    CategoriesListResponse,
    Category,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    OnlineMenuProductsListResponse,
    ReorderOnlineMenuCategoriesRequest,
    ReorderOnlineMenuProductsRequest,
)
from app.services import categories_service
from app.services.billing_service import check_plan_quota_growth
from app.services.operation_events_service import DOMAIN_MENU, record_module_event

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
        SELECT id, name, description, color, tenant_id, created_at, updated_at
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
        INSERT INTO categories (name, description, color, tenant_id)
        VALUES ($1, $2, $3, $4)
        RETURNING id, name, description, color, tenant_id, created_at, updated_at
    """

    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                await check_plan_quota_growth(conn, tenant_id, "menu_categories")
                row = await conn.fetchrow(
                    insert_query,
                    payload.name.strip(),
                    (payload.description.strip() if payload.description else None),
                    payload.color,
                    tenant_id,
                )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una categoría con ese nombre",
        )

    return CategoryResponse(success=True, data=Category(**dict(row)))


# Static paths before /{category_id} (see stations.py:53-55).
@router.get(
    "/online-menu",
    response_model=CategoriesListResponse,
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
)
async def get_online_menu_categories_endpoint(request: Request):
    """Eligible online-menu categories in tenant saved order."""
    return await categories_service.list_online_menu_categories(request)


@router.patch(
    "/online-menu/reorder",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
)
async def reorder_online_menu_categories_endpoint(
    request: Request,
    body: ReorderOnlineMenuCategoriesRequest,
):
    """Persist drag order for public online menu category chips."""
    return await categories_service.reorder_online_menu_categories(request, body.category_ids)


@router.get(
    "/online-menu/products",
    response_model=OnlineMenuProductsListResponse,
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
)
async def get_online_menu_products_endpoint(
    request: Request,
    category_id: UUID = Query(..., description="Category to list eligible online/QR products for"),
):
    """Eligible products in a category, in saved online-menu product order."""
    return await categories_service.list_online_menu_products(request, category_id)


@router.patch(
    "/online-menu/products/reorder",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
)
async def reorder_online_menu_products_endpoint(
    request: Request,
    body: ReorderOnlineMenuProductsRequest,
):
    """Persist drag order for products within one online-menu category."""
    return await categories_service.reorder_online_menu_products(
        request,
        body.category_id,
        body.product_ids,
    )


async def _load_owned_category(conn, category_id: UUID, tenant_id: UUID):
    """Fetch a category and reject globals / cross-tenant access.

    Returns the row when it belongs to the caller's tenant; raises 404
    otherwise (we don't leak existence of globals or other tenants' rows).
    """
    row = await conn.fetchrow(
        "SELECT id, tenant_id FROM categories WHERE id = $1",
        category_id,
    )
    if row is None or row["tenant_id"] is None or row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Category not found")
    return row


@router.put(
    "/{category_id}",
    response_model=CategoryResponse,
    dependencies=[Depends(require_module(Module.MENU))],
)
async def update_category_endpoint(
    request: Request,
    category_id: UUID,
    payload: CategoryUpdate,
):
    """
    Update a tenant-owned category. Globals (tenant_id IS NULL) are
    rejected with 404. Returns 409 on duplicate name, mirroring POST.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context is required")

    data = payload.model_dump(exclude_unset=True)
    updates = {}
    if "name" in data and data["name"] is not None:
        updates["name"] = data["name"].strip()
    if "description" in data:
        desc = data["description"]
        updates["description"] = (desc.strip() or None) if isinstance(desc, str) else None
    if "color" in data:
        updates["color"] = data["color"]  # None clears

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses = []
    params = []
    for idx, (field, value) in enumerate(updates.items(), start=1):
        set_clauses.append(f"{field} = ${idx}")
        params.append(value)
    set_clauses.append("updated_at = NOW()")
    params.append(category_id)

    update_query = f"""
        UPDATE categories
        SET {", ".join(set_clauses)}
        WHERE id = ${len(params)}
        RETURNING id, name, description, color, tenant_id, created_at, updated_at
    """

    try:
        async with get_db_connection() as conn:
            await _load_owned_category(conn, category_id, tenant_id)
            row = await conn.fetchrow(update_query, *params)
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una categoría con ese nombre",
        )

    return CategoryResponse(success=True, data=Category(**dict(row)))


@router.get(
    "/{category_id}/delete-impact",
    dependencies=[Depends(require_module(Module.MENU))],
)
async def category_delete_impact_endpoint(request: Request, category_id: UUID):
    """
    Return dependent counts a delete would affect, without side effects.

    `products` is RESTRICT — non-zero count blocks deletion.
    `station_mappings` is ON DELETE CASCADE — will be silently removed.
    The frontend uses both to warn the user before they confirm.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context is required")

    async with get_db_connection() as conn:
        await _load_owned_category(conn, category_id, tenant_id)
        deps = await conn.fetchrow(
            """
            SELECT
                COALESCE((SELECT COUNT(*) FROM product WHERE category_id = $1), 0) AS products,
                COALESCE((SELECT COUNT(*) FROM tenant_category_stations WHERE category_id = $1), 0) AS station_mappings
            """,
            category_id,
        )

    return {
        "success": True,
        "data": {
            "products": int(deps["products"]),
            "station_mappings": int(deps["station_mappings"]),
        },
    }


@router.delete(
    "/{category_id}",
    dependencies=[Depends(require_module(Module.MENU))],
)
async def delete_category_endpoint(
    request: Request,
    category_id: UUID,
    payload: dict = Body(default={}),
):
    """
    Delete a tenant-owned category.

    Requires `reason` in body (Bitácora audit). Blocks deletion (409) when
    `product.category_id` references exist (RESTRICT FK). Station-mapping rows
    cascade-delete automatically.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    reason = (payload or {}).get("reason", "").strip() if isinstance(payload, dict) else ""
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")

    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context is required")

    async with get_db_connection() as conn:
        await _load_owned_category(conn, category_id, tenant_id)

        deps = await conn.fetchrow(
            """
            SELECT
                COALESCE((SELECT COUNT(*) FROM product WHERE category_id = $1), 0) AS products,
                COALESCE((SELECT COUNT(*) FROM tenant_category_stations WHERE category_id = $1), 0) AS station_mappings
            """,
            category_id,
        )

        if deps["products"] > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "category_has_dependents",
                    "counts": {"products": int(deps["products"])},
                    "message": "La categoría tiene productos asociados que impiden su eliminación.",
                },
            )

        try:
            await conn.execute(
                "DELETE FROM categories WHERE id = $1 AND tenant_id = $2",
                category_id,
                tenant_id,
            )
        except asyncpg.exceptions.ForeignKeyViolationError:
            # Defense-in-depth: a future RESTRICT FK we didn't pre-count would
            # land here instead of leaking a generic 500.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "category_has_dependents_unknown",
                    "message": "La categoría tiene registros asociados que impiden su eliminación.",
                },
            )

        await record_module_event(
            conn,
            tenant_id,
            domain=DOMAIN_MENU,
            action="category_deleted",
            actor_user_id=getattr(session_context, "user_id", None),
            entity_type="category",
            entity_id=category_id,
            reason=reason,
        )

    return {
        "success": True,
        "message": "Category deleted successfully",
        "cascaded": {"station_mappings": int(deps["station_mappings"])},
        "reason": reason,
    }
