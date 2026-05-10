"""
Payment Methods Router
CRUD for payment method groups and methods, plus POS read-only endpoint.

Issue: https://github.com/uno0uno/warocol.com/issues/331
"""
from fastapi import APIRouter, Depends, Request
from uuid import UUID
from app.core.permissions import Module, require_module
from app.services import payment_method_service
from app.models.payment_method import (
    CreateGroupRequest,
    PatchGroupRequest,
    CreateMethodRequest,
    PatchMethodRequest,
)

# ── Finanzas management endpoints ─────────────────────────────────────────────
finanzas_router = APIRouter(prefix="/finanzas/metodos-pago", tags=["payment-methods"])


@finanzas_router.get("/grupos")
async def list_groups(request: Request):
    """List all payment method groups visible to the tenant (global defaults + custom)."""
    return await payment_method_service.list_groups(request)


@finanzas_router.post("/grupos")
async def create_group(request: Request, body: CreateGroupRequest):
    """Create a custom payment method group for the tenant."""
    return await payment_method_service.create_group(request, body)


@finanzas_router.patch("/grupos/{group_id}")
async def patch_group(request: Request, group_id: UUID, body: PatchGroupRequest):
    """Update a custom group's name, sort_order, is_active, or triggers_cartera. Returns 403 for defaults."""
    return await payment_method_service.patch_group(request, group_id, body)


@finanzas_router.get("")
async def list_methods(request: Request):
    """List all payment methods (subtypes) for the tenant."""
    return await payment_method_service.list_methods(request)


@finanzas_router.post("")
async def create_method(request: Request, body: CreateMethodRequest):
    """Create a payment method (subtype) under a group."""
    return await payment_method_service.create_method(request, body)


@finanzas_router.patch("/{method_id}")
async def patch_method(request: Request, method_id: UUID, body: PatchMethodRequest):
    """Update a payment method's name, group, sort_order, or is_active."""
    return await payment_method_service.patch_method(request, method_id, body)


@finanzas_router.delete("/{method_id}")
async def delete_method(request: Request, method_id: UUID):
    """Soft delete a payment method (sets is_active = false)."""
    return await payment_method_service.delete_method(request, method_id)


# ── POS read-only endpoint ─────────────────────────────────────────────────────
# NOTE: finanzas_router endpoints above stay UNGATED in this PR — they get
# Depends(require_module(Module.FINANZAS)) in E2.10 (#198). This file gets
# touched twice across the rollout, intentionally per the audit doc §3.
pos_router = APIRouter(prefix="/pos/payment-methods", tags=["pos"])


@pos_router.get("", dependencies=[Depends(require_module(Module.POS))])
async def list_pos_methods(request: Request):
    """POS: returns active groups with their active methods nested inside."""
    return await payment_method_service.list_pos_methods(request)
