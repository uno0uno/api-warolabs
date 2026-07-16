from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.models.warehouse_category import (
    WarehouseCategoriesResponse,
    WarehouseCategoryCreate,
    WarehouseCategoryResponse,
    WarehouseCategoryUpdate,
)
from app.services.warehouse_categories_service import (
    archive_warehouse_category,
    create_warehouse_category,
    list_warehouse_categories,
    rename_warehouse_category,
)

router = APIRouter()


def _tenant_id(request: Request) -> UUID:
    tenant_id = require_valid_session(request).tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant context required")
    return tenant_id


@router.get(
    "",
    response_model=WarehouseCategoriesResponse,
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def get_warehouse_categories(
    request: Request,
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=250),
    include_archived: bool = Query(default=False),
):
    tenant_id = _tenant_id(request)
    async with get_db_connection() as conn:
        rows = await list_warehouse_categories(
            conn,
            tenant_id,
            search=search,
            limit=limit,
            include_archived=include_archived,
        )
    return {"success": True, "total": len(rows), "data": rows}


@router.post(
    "",
    response_model=WarehouseCategoryResponse,
    status_code=201,
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def post_warehouse_category(
    request: Request,
    body: WarehouseCategoryCreate,
):
    tenant_id = _tenant_id(request)
    async with get_db_connection() as conn:
        async with conn.transaction():
            row = await create_warehouse_category(conn, tenant_id, body.name)
    return {"success": True, "data": row}


@router.patch(
    "/{category_id}",
    response_model=WarehouseCategoryResponse,
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def patch_warehouse_category(
    request: Request,
    category_id: UUID,
    body: WarehouseCategoryUpdate,
):
    tenant_id = _tenant_id(request)
    async with get_db_connection() as conn:
        async with conn.transaction():
            row = await rename_warehouse_category(conn, tenant_id, category_id, body.name)
    return {"success": True, "data": row}


@router.patch(
    "/{category_id}/archive",
    response_model=WarehouseCategoryResponse,
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def patch_archive_warehouse_category(
    request: Request,
    category_id: UUID,
):
    tenant_id = _tenant_id(request)
    async with get_db_connection() as conn:
        async with conn.transaction():
            row = await archive_warehouse_category(conn, tenant_id, category_id)
    return {"success": True, "data": row}
