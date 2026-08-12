"""Menu / bodega / recipe-bases / products / modifiers bulk import API (#2254–#2257)."""
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.services import menu_import_service as svc

router = APIRouter()

_MENU_ENTITIES = ("recipe_bases", "products", "modifiers")


async def _assert_job_module(request: Request, job_id: UUID) -> str:
    """Enforce ABASTECIMIENTO for warehouse; MENU for catalog imports."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")
    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            "SELECT entity_type FROM menu_import_jobs WHERE id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    entity = job["entity_type"]
    if entity == "warehouse":
        await require_module(Module.ABASTECIMIENTO)(request)
    elif entity in _MENU_ENTITIES:
        await require_module(Module.MENU)(request)
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported entity_type: {entity}")
    return entity


async def _assert_list_module(request: Request, entity_type: str) -> None:
    if entity_type == "warehouse":
        await require_module(Module.ABASTECIMIENTO)(request)
    elif entity_type in _MENU_ENTITIES:
        await require_module(Module.MENU)(request)
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported entity_type: {entity_type}")


@router.get(
    "/template/warehouse",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def download_warehouse_template():
    return svc.template_streaming_response("warehouse")


@router.get(
    "/template/recipe_bases",
    dependencies=[Depends(require_module(Module.MENU))],
)
async def download_recipe_bases_template():
    return svc.template_streaming_response("recipe_bases")


@router.get(
    "/template/products",
    dependencies=[Depends(require_module(Module.MENU))],
)
async def download_products_template():
    return svc.template_streaming_response("products")


@router.get(
    "/template/modifiers",
    dependencies=[Depends(require_module(Module.MENU))],
)
async def download_modifiers_template():
    return svc.template_streaming_response("modifiers")


@router.get(
    "/incomplete-resale",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def incomplete_resale(request: Request):
    return await svc.list_incomplete_resale_ingredients(request)


@router.get("/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    entity_type: str = Query(..., description="warehouse | recipe_bases | products | modifiers"),
):
    await _assert_list_module(request, entity_type)
    return await svc.list_import_jobs(request, limit=limit, entity_type=entity_type)


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: UUID):
    await _assert_job_module(request, job_id)
    return await svc.get_import_job(request, job_id)


@router.post(
    "/warehouse/upload",
    status_code=201,
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def upload_warehouse(request: Request, file: UploadFile = File(...)):
    return await svc.upload_warehouse_import(request, file)


@router.post(
    "/recipe_bases/upload",
    status_code=201,
    dependencies=[Depends(require_module(Module.MENU))],
)
async def upload_recipe_bases(request: Request, file: UploadFile = File(...)):
    return await svc.upload_recipe_bases_import(request, file)


@router.post(
    "/products/upload",
    status_code=201,
    dependencies=[Depends(require_module(Module.MENU))],
)
async def upload_products(request: Request, file: UploadFile = File(...)):
    return await svc.upload_products_import(request, file)


@router.post(
    "/modifiers/upload",
    status_code=201,
    dependencies=[Depends(require_module(Module.MENU))],
)
async def upload_modifiers(request: Request, file: UploadFile = File(...)):
    return await svc.upload_modifiers_import(request, file)


@router.post("/jobs/{job_id}/dry-run")
async def dry_run(request: Request, job_id: UUID):
    await _assert_job_module(request, job_id)
    return await svc.dry_run_import(request, job_id)


@router.post("/jobs/{job_id}/commit")
async def commit(request: Request, job_id: UUID):
    await _assert_job_module(request, job_id)
    return await svc.commit_import(request, job_id)
