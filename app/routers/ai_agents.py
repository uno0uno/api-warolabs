import hashlib
import hmac
import logging
from typing import AsyncIterator, Dict, Optional, Sequence
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.config import settings
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.services.kali_access_service import is_kali_enabled


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai-agents"])

SALES_SCOPES = ("orders:read",)
FOOD_COST_SCOPES = ("analytics:read", "menu:read", "financial:read")


def _agent_api_base_url() -> str:
    base_url = (settings.agent_api_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent API URL is not configured",
        )
    return base_url


def _internal_signature_secret() -> str:
    secret = (settings.agent_internal_signature_secret or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent API internal signature is not configured",
        )
    return secret


def _timeout() -> httpx.Timeout:
    connect_timeout = settings.agent_api_connect_timeout_seconds
    read_timeout = settings.agent_api_read_timeout_seconds
    return httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=connect_timeout,
        pool=connect_timeout,
    )


def _sign_internal_request(
    method: str,
    path: str,
    request_id: str,
    tenant_id: str,
    profile_id: str,
    member_id: Optional[str],
    scopes: str,
    body: bytes,
) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        [
            method.upper(),
            path,
            request_id,
            tenant_id,
            profile_id,
            member_id or "",
            scopes,
            body_digest,
        ]
    )
    return hmac.new(
        _internal_signature_secret().encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _resolve_member_id(profile_id: str, tenant_id: str) -> Optional[str]:
    async with get_db_connection() as conn:
        member_id = await conn.fetchval(
            """
            SELECT id
            FROM tenant_members
            WHERE user_id = $1
              AND tenant_id = $2
              AND is_active = true
            ORDER BY CASE
                WHEN role = ANY($3::text[]) THEN 0
                ELSE 1
            END
            LIMIT 1
            """,
            profile_id,
            tenant_id,
            list(LEGACY_INTERNAL_TEAM_ROLES),
        )
    return str(member_id) if member_id else None


def _build_internal_headers(
    path: str,
    request_id: str,
    tenant_id: str,
    profile_id: str,
    member_id: Optional[str],
    scopes: str,
    body: bytes,
) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-waro-tenant-id": tenant_id,
        "x-waro-profile-id": profile_id,
        "x-waro-request-id": request_id,
        "x-waro-scopes": scopes,
    }
    if member_id:
        headers["x-waro-member-id"] = member_id
    headers["x-waro-internal-signature"] = _sign_internal_request(
        method="POST",
        path=path,
        request_id=request_id,
        tenant_id=tenant_id,
        profile_id=profile_id,
        member_id=member_id,
        scopes=scopes,
        body=body,
    )
    return headers


async def _iter_upstream_bytes(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()
        await client.aclose()


async def _proxy_agent_stream(
    request: Request,
    upstream_path: str,
    scopes: Sequence[str],
) -> StreamingResponse:
    session = require_valid_session(request)
    if not session.user_id or not session.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid user and tenant session required",
        )

    if not await is_kali_enabled(session.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kali is not enabled for this tenant",
        )

    body = await request.body()
    request_id = request.headers.get("x-waro-request-id") or str(uuid4())
    tenant_id = str(session.tenant_id)
    profile_id = str(session.user_id)
    member_id = await _resolve_member_id(profile_id, tenant_id)
    scope_header = ",".join(scopes)
    base_url = _agent_api_base_url()
    upstream_url = f"{base_url}{upstream_path}"
    headers = _build_internal_headers(
        path=upstream_path,
        request_id=request_id,
        tenant_id=tenant_id,
        profile_id=profile_id,
        member_id=member_id,
        scopes=scope_header,
        body=body,
    )

    client = httpx.AsyncClient(timeout=_timeout())
    try:
        upstream_request = client.build_request(
            "POST",
            upstream_url,
            content=body,
            headers=headers,
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.warning("Agent API stream setup failed request_id=%s error=%s", request_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Agent API stream setup failed",
        ) from exc

    if response.status_code >= 400:
        await response.aread()
        await response.aclose()
        await client.aclose()
        logger.warning(
            "Agent API rejected stream setup request_id=%s status=%s",
            request_id,
            response.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Agent API rejected stream setup",
        )

    return StreamingResponse(
        _iter_upstream_bytes(response, client),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "x-waro-request-id": request_id,
        },
    )


@router.post("/sales/messages/stream", dependencies=[Depends(require_module(Module.VENTAS))])
async def stream_sales_agent(request: Request) -> StreamingResponse:
    return await _proxy_agent_stream(
        request=request,
        upstream_path="/internal/ai/sales/messages/stream",
        scopes=SALES_SCOPES,
    )


@router.post(
    "/food-cost/messages/stream",
    dependencies=[Depends(require_module(Module.ANALITICA))],
)
async def stream_food_cost_agent(request: Request) -> StreamingResponse:
    return await _proxy_agent_stream(
        request=request,
        upstream_path="/internal/ai/food-cost/messages/stream",
        scopes=FOOD_COST_SCOPES,
    )
