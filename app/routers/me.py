from fastapi import APIRouter, Depends, Request
from typing import Optional
from app.core.middleware import SessionContext, require_valid_session
from app.core.permissions import Module, get_enforcement_mode, get_role_modules
from app.database import get_db_connection
from app.models.me import AccessResponse
from app.services.billing_service import (
    STARTER_PLAN_MODULE_VALUES,
    STARTER_PLAN_SLUG,
    get_effective_plan_slug,
)
from app.services.kali_access_service import get_kali_access_features

router = APIRouter()


def _intersect_plan_modules(role_modules, plan_slug: Optional[str]):
    if plan_slug != STARTER_PLAN_SLUG:
        return role_modules
    allowed = {Module(value) for value in STARTER_PLAN_MODULE_VALUES}
    return frozenset(m for m in role_modules if m in allowed)


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
            features={"kali_enabled": False},
        )

    enforcement_mode = await get_enforcement_mode(session.tenant_id)
    features = await get_kali_access_features(session.tenant_id)

    if session.role is None:
        return AccessResponse(
            role=None,
            modules=[],
            enforcement_mode=enforcement_mode,
            features=features,
        )

    modules = await get_role_modules(session.tenant_id, session.role)
    async with get_db_connection() as conn:
        plan_slug = await get_effective_plan_slug(conn, session.tenant_id)
    modules = _intersect_plan_modules(modules, plan_slug)
    if plan_slug == STARTER_PLAN_SLUG:
        features = {**features, "kali_enabled": False}
    return AccessResponse(
        role=session.role,
        modules=sorted(m.value for m in modules),
        plan_slug=plan_slug,
        enforcement_mode=enforcement_mode,
        features=features,
    )
