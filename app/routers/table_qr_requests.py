"""Staff endpoints for Table QR pending requests (api-warolabs#268)."""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.permissions import Module, require_module
from app.services import table_qr_requests_service

router = APIRouter(tags=["Table QR Requests"])


class BulkAcceptRequest(BaseModel):
    request_ids: Optional[List[UUID]] = None
    table_id: Optional[UUID] = None
    all_pending: bool = False


class RejectTableQrRequest(BaseModel):
    reason: str = Field(..., min_length=1)


@router.get("", dependencies=[Depends(require_module(Module.DESPACHO))])
async def list_table_qr_requests(
    request: Request,
    status: str = Query(default="pending", pattern="^(pending|accepted|rejected|all)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    table_id: Optional[UUID] = Query(None),
    grouped: Optional[bool] = Query(
        None,
        description="Legacy tables[] grouping. Defaults to true when status=pending.",
    ),
):
    """List Table QR requests for Despacho (filter + pagination)."""
    use_grouped = grouped if grouped is not None else (status == "pending")
    return await table_qr_requests_service.list_requests(
        request,
        status=status,
        limit=limit,
        offset=offset,
        table_id=table_id,
        grouped=use_grouped,
    )


@router.get("/{request_id}", dependencies=[Depends(require_module(Module.DESPACHO))])
async def get_table_qr_request(request: Request, request_id: UUID):
    """Get a single Table QR request (any status) for Despacho detail."""
    return await table_qr_requests_service.get_request(request, request_id)


@router.patch("/{request_id}/reject", dependencies=[Depends(require_module(Module.DESPACHO))])
async def reject_table_qr_request(request: Request, request_id: UUID, body: RejectTableQrRequest):
    return await table_qr_requests_service.reject_request(request, request_id, body.reason)


@router.patch("/{request_id}/accept", dependencies=[Depends(require_module(Module.DESPACHO))])
async def accept_table_qr_request(request: Request, request_id: UUID):
    return await table_qr_requests_service.accept_requests(request, [request_id])


@router.post("/bulk-accept", dependencies=[Depends(require_module(Module.DESPACHO))])
async def bulk_accept_table_qr_requests(request: Request, body: BulkAcceptRequest):
    return await table_qr_requests_service.bulk_accept(
        request,
        request_ids=body.request_ids,
        table_id=body.table_id,
        all_pending=body.all_pending,
    )
