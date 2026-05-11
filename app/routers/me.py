from fastapi import APIRouter, Depends, Request
from app.core.middleware import SessionContext, require_valid_session
from app.core.permissions import get_enforcement_mode, get_role_modules
from app.models.me import AccessResponse

router = APIRouter()


@router.get("/access", response_model=AccessResponse)
async def get_my_access(
    request: Request,
    session: SessionContext = Depends(require_valid_session),
) -> AccessResponse:
    """Return effective access map for the current session.

    Drives Epic 4 frontend sidebar / route gating. Always reachable by any
    authenticated user — no Module gate on purpose: this endpoint IS the
    source of truth for those gates and cannot gate itself.

    Short-circuits when `session.tenant_id` or `session.role` is None so
    edge-case sessions (fresh-tenant owners pre-membership, KDS-token
    synthetic sessions) get a coherent empty-modules response instead of
    a crash from `get_role_modules`.
    """
    if session.tenant_id is None:
        return AccessResponse(
            role=session.role,
            modules=[],
            enforcement_mode="disabled",
        )

    enforcement_mode = await get_enforcement_mode(session.tenant_id)

    if session.role is None:
        return AccessResponse(
            role=None,
            modules=[],
            enforcement_mode=enforcement_mode,
        )

    modules = await get_role_modules(session.tenant_id, session.role)
    return AccessResponse(
        role=session.role,
        modules=sorted(m.value for m in modules),
        enforcement_mode=enforcement_mode,
    )
