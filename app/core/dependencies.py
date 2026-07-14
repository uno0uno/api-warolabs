from typing import Any, Dict

from fastapi import Request, HTTPException
from app.core.tenant import detect_and_validate_tenant
from app.core.security import get_session_token
# from app.services.auth_service import get_session_data  # Will implement in Day 3

async def get_current_session(request: Request):
    """Dependency to get current session - will implement in Day 3"""
    # Placeholder - will implement actual session validation
    session_token = await get_session_token(request)
    return {"session_token": session_token, "placeholder": True}

async def get_tenant_context(request: Request):
    """Dependency to get tenant context"""
    return await detect_and_validate_tenant(request)

# Placeholder for now - will implement in Day 3
async def require_auth(request: Request):
    """Dependency that requires authentication"""
    session_token = await get_session_token(request)
    # TODO: Validate session against database
    return session_token


async def require_invoicing_ready(request: Request) -> Dict[str, Any]:
    """
    Gate dependency for electronic-invoice emission endpoints (issue #130).

    Resolves the active session, calls the invoicing-readiness service, and
    raises 403 with a structured payload when the tenant is not ready. Returns
    the readiness dict on success so handlers can read `checks` if needed.
    """
    from app.core.middleware import require_valid_session
    from app.services import invoicing_readiness_service

    session = require_valid_session(request)
    payload = await invoicing_readiness_service.get_readiness(session.tenant_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not payload['ready']:
        raise HTTPException(
            status_code=403,
            detail={
                'error':   'tenant_not_ready_for_invoicing',
                'checks':  payload['checks'],
                'reason_codes': payload.get('reason_codes', []),
                'missing': payload['missing'],
            },
        )
    return payload
