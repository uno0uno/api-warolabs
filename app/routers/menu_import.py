"""Menu / bodega bulk import API (#2254)."""
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from app.core.permissions import Module, require_module
from app.services import menu_import_service as svc

router = APIRouter()


@router.get(
    "/template/warehouse",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def download_warehouse_template():
    return svc.template_streaming_response()


@router.get(
    "/jobs",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def list_jobs(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    return await svc.list_import_jobs(request, limit=limit)


@router.get(
    "/jobs/{job_id}",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
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
    "/jobs/{job_id}/dry-run",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def dry_run(request: Request, job_id: UUID):
    return await svc.dry_run_warehouse_import(request, job_id)


@router.post(
    "/jobs/{job_id}/commit",
    dependencies=[Depends(require_module(Module.ABASTECIMIENTO))],
)
async def commit(request: Request, job_id: UUID):
    return await svc.commit_warehouse_import(request, job_id)
