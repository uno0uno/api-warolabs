"""Menu / bodega / recipe-bases bulk import API (#2254, #2255)."""
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from app.core.permissions import Module, require_any_module, require_module
from app.services import menu_import_service as svc

router = APIRouter()

_IMPORT_READ = Depends(require_any_module(Module.ABASTECIMIENTO, Module.MENU))


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


@router.get("/jobs", dependencies=[_IMPORT_READ])
async def list_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    entity_type: str | None = Query(default=None),
):
    return await svc.list_import_jobs(request, limit=limit, entity_type=entity_type)


@router.get("/jobs/{job_id}", dependencies=[_IMPORT_READ])
async def get_job(request: Request, job_id: UUID):
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


@router.post("/jobs/{job_id}/dry-run", dependencies=[_IMPORT_READ])
async def dry_run(request: Request, job_id: UUID):
    return await svc.dry_run_import(request, job_id)


@router.post("/jobs/{job_id}/commit", dependencies=[_IMPORT_READ])
async def commit(request: Request, job_id: UUID):
    return await svc.commit_import(request, job_id)
