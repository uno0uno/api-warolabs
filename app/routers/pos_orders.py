"""POS-scoped order invoice endpoints for checkout (cashier RBAC)."""
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.dependencies import require_invoicing_ready
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.services import facturacion_service

router = APIRouter(prefix="/pos/orders", tags=["pos"])


@router.post(
    "/{order_id}/invoice",
    tags=["Invoices"],
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_emit_order_invoice(
    request: Request,
    order_id: UUID,
    _readiness: dict = Depends(require_invoicing_ready),
):
    session_context = require_valid_session(request)
    return await facturacion_service.emit_invoice(
        order_id=str(order_id),
        tenant_id=str(session_context.tenant_id),
        order_type="pos",
    )


@router.get(
    "/{order_id}/invoice",
    tags=["Invoices"],
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_get_order_invoice(
    request: Request,
    order_id: UUID,
):
    session_context = require_valid_session(request)
    result = await facturacion_service.get_order_invoice(
        order_id=str(order_id),
        tenant_id=str(session_context.tenant_id),
    )
    if result is None:
        return None
    return result
