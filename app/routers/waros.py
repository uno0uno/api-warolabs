"""
Waros Points System Router
Endpoints for configuring earning rules and global system toggle.
All endpoints require a valid tenant session.
"""
import json
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.permissions import Module, require_module
from app.services import waros_service

router = APIRouter(prefix="/admin/waros", tags=["Waros"])

VALID_RULE_TYPES = {"ticket_value", "purchase_count", "frequency", "per_ticket_qty"}
VALID_REWARD_TYPES = {"fixed_cop_off", "free_product"}


# ── Pydantic models ──────────────────────────────────────────────────────────
# Python 3.9 safe: Optional[X] from typing, no X | None syntax

class RuleBody(BaseModel):
    """Body for PUT /admin/waros/rules/{rule_type}"""
    is_active: bool
    config: Dict[str, Any] = {}


class GlobalConfigBody(BaseModel):
    """Body for PATCH /admin/waros/config"""
    is_enabled: bool


class RedemptionConfigBody(BaseModel):
    """Body for PATCH /admin/waros/redemption-config"""
    is_enabled: Optional[bool] = None
    redemption_enabled: Optional[bool] = None
    waros_per_1000_cop: Optional[int] = Field(None, ge=1)
    max_redeem_percent_per_order: Optional[float] = Field(None, ge=0, le=100)
    min_waros_to_redeem: Optional[int] = Field(None, ge=1)
    earn_on_wallet_payment: Optional[bool] = None
    earn_base_excludes_waro_redemption: Optional[bool] = None


class WaroRewardBody(BaseModel):
    name: str
    reward_type: str
    waros_cost: int = Field(..., ge=1)
    fixed_cop_off: Optional[float] = None
    product_id: Optional[UUID] = None
    is_active: bool = True


class WaroRewardUpdateBody(BaseModel):
    name: Optional[str] = None
    waros_cost: Optional[int] = Field(None, ge=1)
    fixed_cop_off: Optional[float] = None
    product_id: Optional[UUID] = None
    is_active: Optional[bool] = None


class AssignBody(BaseModel):
    """Body for POST /admin/waros/assign"""
    profile_id: UUID
    waros_amount: int
    reason: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/rules", dependencies=[Depends(require_module(Module.POS))])
async def get_rules(request: Request):
    """
    Get all earning rules for the tenant.
    Always returns all 4 rule types (with defaults for unconfigured ones).
    Also returns the global is_enabled flag.
    """
    return await waros_service.get_rules(request)


@router.put("/rules/{rule_type}", dependencies=[Depends(require_module(Module.POS))])
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


@router.patch("/rules/{rule_type}/toggle", dependencies=[Depends(require_module(Module.POS))])
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


@router.patch("/config", dependencies=[Depends(require_module(Module.POS))])
async def update_global_config(body: GlobalConfigBody, request: Request):
    """
    Enable or disable the entire Waros system for the tenant.
    Upserts gamification_config.is_enabled.
    """
    return await waros_service.update_global_config(request, is_enabled=body.is_enabled)


@router.get("/estimate", dependencies=[Depends(require_module(Module.POS))])
async def estimate_waros(
    request: Request,
    total_amount: float = Query(..., description="Cart total in COP", gt=0),
    customer_id: Optional[str] = Query(None, description="Customer profile UUID (optional)"),
    payment_method: Optional[str] = Query(
        None,
        description="Payment method slug (e.g. customer_wallet). When wallet and earn_on_wallet_payment=false, returns 0.",
    ),
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
    return await waros_service.estimate_waros(request, total_amount, parsed_customer_id, payment_method)


@router.get("/customers/balances", dependencies=[Depends(require_module(Module.POS))])
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


@router.get("/customers/{profile_id}/summary", dependencies=[Depends(require_module(Module.POS))])
async def get_customer_summary(profile_id: UUID, request: Request):
    """
    Waros summary for a single customer: balance + last 5 transactions.
    Returns 0 balance and empty transactions if the customer has no wallet.
    """
    return await waros_service.get_customer_summary(request, profile_id)


@router.post("/assign", dependencies=[Depends(require_module(Module.POS))])
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


@router.get("/redemption-config", dependencies=[Depends(require_module(Module.POS))])
async def get_redemption_config(request: Request):
    """Redemption + earn flags from gamification_config (api#370)."""
    return await waros_service.get_redemption_config(request)


@router.patch("/redemption-config", dependencies=[Depends(require_module(Module.POS))])
async def patch_redemption_config(body: RedemptionConfigBody, request: Request):
    return await waros_service.update_redemption_config(
        request,
        redemption_enabled=body.redemption_enabled,
        waros_per_1000_cop=body.waros_per_1000_cop,
        max_redeem_percent_per_order=body.max_redeem_percent_per_order,
        min_waros_to_redeem=body.min_waros_to_redeem,
        earn_on_wallet_payment=body.earn_on_wallet_payment,
        earn_base_excludes_waro_redemption=body.earn_base_excludes_waro_redemption,
        is_enabled=body.is_enabled,
    )


@router.get("/preview-redemption", dependencies=[Depends(require_module(Module.POS))])
async def preview_redemption(
    request: Request,
    lines: str = Query(..., description="JSON array of cart line objects for promo evaluation"),
    customer_id: Optional[str] = Query(None),
    manual_discount_amount: float = Query(0, ge=0),
    discount_type: Optional[str] = Query(None),
    discount_value: Optional[float] = Query(None),
    waros_to_redeem: Optional[int] = Query(None, ge=0),
    waro_reward_id: Optional[str] = Query(None),
):
    try:
        parsed_lines = json.loads(lines)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="lines debe ser JSON válido")
    if not isinstance(parsed_lines, list):
        raise HTTPException(status_code=422, detail="lines debe ser un array JSON")

    parsed_customer: Optional[UUID] = None
    if customer_id:
        try:
            parsed_customer = UUID(customer_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="customer_id no es un UUID válido")

    parsed_reward: Optional[UUID] = None
    if waro_reward_id:
        try:
            parsed_reward = UUID(waro_reward_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="waro_reward_id no es un UUID válido")

    return await waros_service.preview_redemption(
        request,
        parsed_lines,
        customer_id=parsed_customer,
        manual_discount_amount=manual_discount_amount,
        discount_type=discount_type,
        discount_value=discount_value,
        waros_to_redeem=waros_to_redeem,
        waro_reward_id=parsed_reward,
    )


@router.get("/rewards", dependencies=[Depends(require_module(Module.POS))])
async def list_rewards(request: Request):
    return await waros_service.list_waro_rewards(request)


@router.post("/rewards", dependencies=[Depends(require_module(Module.POS))])
async def create_reward(body: WaroRewardBody, request: Request):
    if body.reward_type not in VALID_REWARD_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"reward_type inválido. Valores: {sorted(VALID_REWARD_TYPES)}",
        )
    return await waros_service.create_waro_reward(
        request,
        name=body.name,
        reward_type=body.reward_type,
        waros_cost=body.waros_cost,
        fixed_cop_off=body.fixed_cop_off,
        product_id=body.product_id,
        is_active=body.is_active,
    )


@router.put("/rewards/{reward_id}", dependencies=[Depends(require_module(Module.POS))])
async def update_reward(reward_id: UUID, body: WaroRewardUpdateBody, request: Request):
    return await waros_service.update_waro_reward(
        request,
        reward_id,
        name=body.name,
        waros_cost=body.waros_cost,
        fixed_cop_off=body.fixed_cop_off,
        product_id=body.product_id,
        is_active=body.is_active,
    )


@router.delete("/rewards/{reward_id}", dependencies=[Depends(require_module(Module.POS))])
async def delete_reward(reward_id: UUID, request: Request):
    return await waros_service.delete_waro_reward(request, reward_id)
