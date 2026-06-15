from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.core.security import get_client_ip
from app.database import get_db_connection
from app.services import legal_service


router = APIRouter(prefix="/legal", tags=["legal"])


class AcceptTermsBody(BaseModel):
    source: Optional[str] = "api"


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


@router.post("/terms/accept", status_code=201)
async def accept_terms(body: AcceptTermsBody, request: Request):
    session = require_valid_session(request)
    async with get_db_connection() as conn:
        return await legal_service.accept_current_terms(
            conn,
            session,
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            source=body.source or "api",
        )
