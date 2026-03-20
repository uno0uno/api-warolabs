"""
Waros Points System Router
Endpoints for configuring earning rules and global system toggle.
All endpoints require a valid tenant session.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

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
