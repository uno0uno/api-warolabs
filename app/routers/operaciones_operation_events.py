"""Bitácora de operaciones — list POS audit events (warocol.com#782)."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.permissions import Module, require_module
from app.services import operation_events_service

router = APIRouter(prefix="/operaciones", tags=["Operaciones Bitácora"])


@router.get(
    "/operation-events",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def list_operation_events_endpoint(
    request: Request,
    domain: str = Query("pos", description="Event domain (MVP: pos)"),
    date_from: Optional[str] = Query(None, description="ISO date YYYY-MM-DD (inclusive, tenant local day)"),
    date_to: Optional[str] = Query(None, description="ISO date YYYY-MM-DD (inclusive, tenant local day)"),
    channel: Optional[str] = Query(None, description="mesa | barra | mostrador"),
    action: Optional[str] = Query(None, description="Action filter (see epic catalog)"),
    actor_user_id: Optional[UUID] = Query(None),
    q: Optional[str] = Query(None, description="Text search on payload JSON"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Paginated operation audit log for the current tenant."""
    return await operation_events_service.list_operation_events(
        request,
        domain=domain,
        date_from=date_from,
        date_to=date_to,
        channel=channel,
        action=action,
        actor_user_id=actor_user_id,
        q=q,
        limit=limit,
        offset=offset,
    )
