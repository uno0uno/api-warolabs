"""Admin billing quota overrides — superuser only (#2705)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.database import get_db_connection

router = APIRouter(prefix="/admin/billing", tags=["Admin Billing"])


class QuotaOverrideBody(BaseModel):
    tenant_id: str
    limit: int
    disabled: bool = False


@router.post("/quota-override")
async def set_quota_override(body: QuotaOverrideBody, request: Request):
    require_valid_session(request)
    async with get_db_connection() as conn:
        if body.disabled:
            await conn.execute(
                """
                INSERT INTO tenant_quota_overrides (tenant_id, resource, limit_override, disabled)
                VALUES ($1, 'electronic_invoices_per_period', NULL, true)
                ON CONFLICT (tenant_id, resource) DO UPDATE SET limit_override=NULL, disabled=true, updated_at=now()
                """,
                body.tenant_id,
            )
        else:
            await conn.execute(
                """
                INSERT INTO tenant_quota_overrides (tenant_id, resource, limit_override, disabled)
                VALUES ($1, 'electronic_invoices_per_period', $2, false)
                ON CONFLICT (tenant_id, resource) DO UPDATE SET limit_override=$2, disabled=false, updated_at=now()
                """,
                body.tenant_id,
                body.limit,
            )
    return {"tenant_id": body.tenant_id, "limit": body.limit, "disabled": body.disabled}


@router.get("/quota-overrides/{tenant_id}")
async def get_quota_overrides(tenant_id: str, request: Request):
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, resource, limit_override, disabled FROM tenant_quota_overrides WHERE tenant_id=$1",
            tenant_id,
        )
        return {"overrides": [dict(r) for r in rows]}
