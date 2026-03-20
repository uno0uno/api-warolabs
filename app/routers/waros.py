"""
Waros Points System Router
Endpoints for configuring earning rules and global system toggle.
All endpoints require a valid tenant session.
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services import waros_service

router = APIRouter(prefix="/admin/waros", tags=["Waros"])

VALID_RULE_TYPES = {"ticket_value", "purchase_count", "frequency", "per_ticket_qty"}


# ── Pydantic models ──────────────────────────────────────────────────────────
# Python 3.9 safe: Optional[X] from typing, no X | None syntax

class RuleBody(BaseModel):
    """Body for PUT /admin/waros/rules/{rule_type}"""
    is_active: bool
    config: Dict[str, Any] = {}


class GlobalConfigBody(BaseModel):
    """Body for PATCH /admin/waros/config"""
    is_enabled: bool


class AssignBody(BaseModel):
    """Body for POST /admin/waros/assign"""
    profile_id: UUID
    waros_amount: int
    reason: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/rules")
async def get_rules(request: Request):
    """
    Get all earning rules for the tenant.
    Always returns all 4 rule types (with defaults for unconfigured ones).
    Also returns the global is_enabled flag.
    """
    return await waros_service.get_rules(request)


@router.put("/rules/{rule_type}")
async def upsert_rule(rule_type: str, body: RuleBody, request: Request):
    """
    Create or update an earning rule for the tenant.
    rule_type must be one of: ticket_value, purchase_count, frequency, per_ticket_qty
    """
    if rule_type not in VALID_RULE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"rule_type inválido. Valores permitidos: {sorted(VALID_RULE_TYPES)}",
        )
    return await waros_service.upsert_rule(
        request,
        rule_type=rule_type,
        is_active=body.is_active,
        config=body.config,
    )


@router.patch("/rules/{rule_type}/toggle")
async def toggle_rule(rule_type: str, request: Request):
    """
    Toggle is_active for a rule without changing its config.
    Creates the rule with defaults if it doesn't exist yet.
    """
    if rule_type not in VALID_RULE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"rule_type inválido. Valores permitidos: {sorted(VALID_RULE_TYPES)}",
        )
    return await waros_service.toggle_rule(request, rule_type=rule_type)


@router.patch("/config")
async def update_global_config(body: GlobalConfigBody, request: Request):
    """
    Enable or disable the entire Waros system for the tenant.
    Upserts gamification_config.is_enabled.
    """
    return await waros_service.update_global_config(request, is_enabled=body.is_enabled)


@router.get("/estimate")
async def estimate_waros(
    request: Request,
    total_amount: float = Query(..., description="Cart total in COP", gt=0),
    customer_id: Optional[str] = Query(None, description="Customer profile UUID (optional)"),
):
    """
    Read-only estimate of Waros that would be earned for an order with the given total.
    - Never writes to DB.
    - per_ticket_qty always returns 0 (item count unknown before order is placed).
    - purchase_count and frequency simulate +1 completed order.
    - customer_id is optional: omit it to estimate ticket_value rule only.
    """
    parsed_customer_id: Optional[UUID] = None
    if customer_id:
        try:
            parsed_customer_id = UUID(customer_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="customer_id no es un UUID válido")
    return await waros_service.estimate_waros(request, total_amount, parsed_customer_id)


@router.get("/customers/balances")
async def get_customers_balances(
    request: Request,
    profile_ids: str = Query(..., description="Comma-separated profile UUIDs (max 500)"),
):
    """
    Batch Waros balances for a list of customers.
    Returns { "balances": { "uuid": int, ... } } — missing wallets return 0.
    Must be defined BEFORE /customers/{profile_id}/summary to avoid route conflict.
    """
    try:
        ids: List[UUID] = [
            UUID(p.strip()) for p in profile_ids.split(",") if p.strip()
        ]
    except ValueError:
        raise HTTPException(status_code=422, detail="Uno o más profile_ids no son UUIDs válidos")

    if not ids:
        raise HTTPException(status_code=422, detail="profile_ids no puede estar vacío")

    if len(ids) > 500:
        raise HTTPException(status_code=422, detail="Máximo 500 profile_ids por consulta")

    return await waros_service.get_customers_balances(request, ids)


@router.get("/customers/{profile_id}/summary")
async def get_customer_summary(profile_id: UUID, request: Request):
    """
    Waros summary for a single customer: balance + last 5 transactions.
    Returns 0 balance and empty transactions if the customer has no wallet.
    """
    return await waros_service.get_customer_summary(request, profile_id)


@router.post("/assign")
async def assign_waros(body: AssignBody, request: Request):
    """
    Manually award or deduct Waros from a customer.
    - waros_amount > 0: award points
    - waros_amount < 0: deduct points (cannot go below 0)
    - assigned_by is taken from the authenticated admin session
    """
    if body.waros_amount == 0:
        raise HTTPException(status_code=422, detail="waros_amount no puede ser 0")
    return await waros_service.assign_manual_waros(
        request,
        profile_id=body.profile_id,
        waros_amount=body.waros_amount,
        reason=body.reason,
    )
