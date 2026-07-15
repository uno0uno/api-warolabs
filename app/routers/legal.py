from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.core.security import get_client_ip
from app.database import get_db_connection
from app.services import legal_service, onboarding_service


router = APIRouter(prefix="/legal", tags=["legal"])


class AcceptTermsBody(BaseModel):
    source: Optional[str] = "api"


def _require_session_tenant_id(session) -> UUID:
    if not session.tenant_id:
        raise HTTPException(status_code=401, detail="Tenant ID is required")
    return session.tenant_id


# Global-auth exception: tenants must be able to read/accept current terms
# before other module-gated flows are available.
@router.get("/terms/current")
async def get_current_terms(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        current = await legal_service.get_current_terms(conn, session.tenant_id)
        return {"success": True, "data": current}


@router.get("/terms/status")
async def get_terms_status(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await legal_service.get_terms_status(conn, session.tenant_id)


@router.get("/terms/audit", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
async def list_terms_acceptance_audit(
    request: Request,
    document_version_id: Optional[UUID] = None,
    actor_email: Optional[str] = None,
    accepted_from: Optional[datetime] = None,
    accepted_to: Optional[datetime] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    session = require_valid_session(request)
    tenant_id = _require_session_tenant_id(session)
    async with get_db_connection(use_transaction=False) as conn:
        return await legal_service.list_acceptance_audit_records(
            conn,
            tenant_id,
            document_version_id=document_version_id,
            actor_email=actor_email,
            accepted_from=accepted_from,
            accepted_to=accepted_to,
            limit=limit,
            offset=offset,
        )


@router.get("/terms/audit/{acceptance_id}", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
async def get_terms_acceptance_audit(acceptance_id: UUID, request: Request):
    session = require_valid_session(request)
    tenant_id = _require_session_tenant_id(session)
    async with get_db_connection(use_transaction=False) as conn:
        return await legal_service.get_acceptance_audit_record(conn, tenant_id, acceptance_id)


@router.post("/terms/accept", status_code=201)
async def accept_terms(body: AcceptTermsBody, request: Request):
    session = require_valid_session(request)
    async with get_db_connection() as conn:
        if session.lifecycle_status == "pending":
            return await onboarding_service.accept_onboarding_terms(
                conn,
                session,
                client_ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        return await legal_service.accept_current_terms(
            conn,
            session,
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            source=body.source or "api",
        )
