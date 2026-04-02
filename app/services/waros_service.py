"""
Waros Points System Service
Business logic for configurable earning rules and manual assignments.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, Request

from app.core.middleware import require_valid_session
from app.database import get_db_connection

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
# Returned when a rule type has no row in waro_earning_rules yet.
# Ensures GET /admin/waros/rules always returns all 4 types.

RULE_TYPES = ["ticket_value", "purchase_count", "frequency", "per_ticket_qty"]

RULE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "ticket_value": {
        "rule_name": "Por valor de compra",
        "is_active": False,
        "config": {"base_waros_per_1000": 1, "tiers": []},
    },
    "purchase_count": {
        "rule_name": "Por número de compras",
        "is_active": False,
        "config": {"milestones": []},
    },
    "frequency": {
        "rule_name": "Por frecuencia",
        "is_active": False,
        "config": {"purchases": 2, "within_days": 60, "bonus_waros": 75},
    },
    "per_ticket_qty": {
        "rule_name": "Por boletas compradas",
        "is_active": False,
        "config": {"waros_per_ticket": 10, "bonus_thresholds": []},
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Convert asyncpg Record to plain dict, parsing JSONB config."""
    d = dict(row)
    if "config" in d and isinstance(d["config"], str):
        d["config"] = json.loads(d["config"])
    return d


# ── Service functions ────────────────────────────────────────────────────────

async def get_rules(request: Request) -> Dict[str, Any]:
    """
    GET /admin/waros/rules
    Returns all 4 rule types for the tenant, with defaults for missing ones.
    Also returns the global is_enabled flag from gamification_config.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection(use_transaction=False) as conn:
            # Fetch existing rules
            rows = await conn.fetch(
                """
                SELECT rule_type, rule_name, is_active, config
                FROM waro_earning_rules
                WHERE tenant_id = $1
                """,
                tenant_id,
            )

            # Fetch global config
            config_row = await conn.fetchrow(
                """
                SELECT is_enabled
                FROM gamification_config
                WHERE tenant_id = $1
                """,
                tenant_id,
            )

        existing = {row["rule_type"]: _row_to_dict(row) for row in rows}

        # Merge DB rows with defaults — always return all 4 types
        rules = []
        for rule_type in RULE_TYPES:
            if rule_type in existing:
                rules.append({"rule_type": rule_type, **existing[rule_type]})
            else:
                rules.append({"rule_type": rule_type, **RULE_DEFAULTS[rule_type]})

        return {
            "is_enabled": config_row["is_enabled"] if config_row else False,
            "rules": rules,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_rules: {e}")
        raise HTTPException(status_code=500, detail="Error al obtener reglas de puntos")


async def upsert_rule(
    request: Request,
    rule_type: str,
    is_active: bool,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    PUT /admin/waros/rules/{rule_type}
    Creates or updates a rule. rule_type is validated in the router.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        rule_name = RULE_DEFAULTS[rule_type]["rule_name"]
        config_json = json.dumps(config)

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO waro_earning_rules
                    (tenant_id, rule_type, rule_name, is_active, config)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (tenant_id, rule_type)
                DO UPDATE SET
                    is_active  = EXCLUDED.is_active,
                    config     = EXCLUDED.config,
                    updated_at = now()
                RETURNING rule_type, rule_name, is_active, config
                """,
                tenant_id,
                rule_type,
                rule_name,
                is_active,
                config_json,
            )

        return {"rule_type": rule_type, **_row_to_dict(row)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in upsert_rule ({rule_type}): {e}")
        raise HTTPException(status_code=500, detail="Error al guardar la regla")


async def toggle_rule(request: Request, rule_type: str) -> Dict[str, Any]:
    """
    PATCH /admin/waros/rules/{rule_type}/toggle
    Flips is_active without touching config.
    Creates the row with defaults if it doesn't exist yet.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        rule_name = RULE_DEFAULTS[rule_type]["rule_name"]
        default_config = json.dumps(RULE_DEFAULTS[rule_type]["config"])

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO waro_earning_rules
                    (tenant_id, rule_type, rule_name, is_active, config)
                VALUES ($1, $2, $3, false, $4::jsonb)
                ON CONFLICT (tenant_id, rule_type)
                DO UPDATE SET
                    is_active  = NOT waro_earning_rules.is_active,
                    updated_at = now()
                RETURNING rule_type, rule_name, is_active, config
                """,
                tenant_id,
                rule_type,
                rule_name,
                default_config,
            )

        return {"rule_type": rule_type, **_row_to_dict(row)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in toggle_rule ({rule_type}): {e}")
        raise HTTPException(status_code=500, detail="Error al cambiar estado de la regla")


async def update_global_config(request: Request, is_enabled: bool) -> Dict[str, Any]:
    """
    PATCH /admin/waros/config
    Enables or disables the entire Waros system for the tenant.
    Upserts gamification_config.is_enabled.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO gamification_config (tenant_id, is_enabled)
                VALUES ($1, $2)
                ON CONFLICT (tenant_id)
                DO UPDATE SET
                    is_enabled = EXCLUDED.is_enabled,
                    updated_at = now()
                RETURNING tenant_id, is_enabled
                """,
                tenant_id,
                is_enabled,
            )

        return {"tenant_id": str(row["tenant_id"]), "is_enabled": row["is_enabled"]}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in update_global_config: {e}")
        raise HTTPException(status_code=500, detail="Error al actualizar configuración global")


# ── Customer read endpoints ──────────────────────────────────────────────────

async def _get_customer_summary_for_tenant(
    tenant_id: str,
    profile_id: UUID,
) -> Dict[str, Any]:
    """Auth-agnostic core for customer summary. Called by session wrapper and public API."""
    async with get_db_connection(use_transaction=False) as conn:
        wallet_row = await conn.fetchrow(
            """
            SELECT current_balance, lifetime_earned, lifetime_spent
            FROM waros_wallets
            WHERE profile_id = $1 AND tenant_id = $2
            """,
            profile_id,
            tenant_id,
        )

        tx_rows = await conn.fetch(
            """
            SELECT id, created_at, transaction_type, waros_amount,
                   description, related_entity_type, related_entity_id
            FROM waros_transactions
            WHERE profile_id = $1 AND tenant_id = $2
            ORDER BY created_at DESC
            LIMIT 5
            """,
            profile_id,
            tenant_id,
        )

    transactions = [
        {
            "id": row["id"],
            "created_at": row["created_at"].isoformat(),
            "transaction_type": row["transaction_type"],
            "waros_amount": row["waros_amount"],
            "description": row["description"],
            "related_entity_type": row["related_entity_type"],
            "related_entity_id": row["related_entity_id"],
        }
        for row in tx_rows
    ]

    return {
        "profile_id": str(profile_id),
        "current_balance": int(wallet_row["current_balance"]) if wallet_row else 0,
        "lifetime_earned": int(wallet_row["lifetime_earned"]) if wallet_row else 0,
        "lifetime_spent": int(wallet_row["lifetime_spent"]) if wallet_row else 0,
        "recent_transactions": transactions,
    }



async def get_customer_summary(
    request: Request,
    profile_id: UUID,
) -> Dict[str, Any]:
    """
    GET /admin/waros/customers/{profile_id}/summary
    Returns wallet balance + last 5 transactions for a customer.
    Returns 0 balance and empty transactions if no wallet exists (never 404).
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        return await _get_customer_summary_for_tenant(tenant_id, profile_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_customer_summary (profile={profile_id}): {e}")
        raise HTTPException(status_code=500, detail="Error al obtener resumen de Waros")


async def _get_customers_balances_for_tenant(
    tenant_id: str,
    profile_ids: List[UUID],
) -> Dict[str, Any]:
    """Auth-agnostic core for batch balances. Called by session wrapper and public API."""
    # Initialise all requested IDs to 0
    balances: Dict[str, int] = {str(pid): 0 for pid in profile_ids}

    if not profile_ids:
        return {"balances": balances}

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT profile_id, COALESCE(current_balance, 0) AS current_balance
            FROM waros_wallets
            WHERE profile_id = ANY($1::uuid[]) AND tenant_id = $2
            """,
            profile_ids,
            tenant_id,
        )

    for row in rows:
        balances[str(row["profile_id"])] = int(row["current_balance"])

    return {"balances": balances}



async def get_customers_balances(
    request: Request,
    profile_ids: List[UUID],
) -> Dict[str, Any]:
    """
    GET /admin/waros/customers/balances?profile_ids=uuid1,uuid2,...
    Batch query: returns {profile_id: balance} map for a list of customers.
    Profiles without a wallet return 0. Missing IDs also return 0.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        return await _get_customers_balances_for_tenant(tenant_id, profile_ids)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_customers_balances: {e}")
        raise HTTPException(status_code=500, detail="Error al obtener balances de Waros")


# ── Manual assignment ────────────────────────────────────────────────────────

async def assign_manual_waros(
    request: Request,
    profile_id: UUID,
    waros_amount: int,
    reason: Optional[str],
) -> Dict[str, Any]:
    """
    POST /admin/waros/assign
    Manually award (positive) or deduct (negative) Waros for a customer.
    Atomic write: upsert wallet + audit log + transaction.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        admin_user_id = session.user_id  # profile.id of the logged-in admin

        # Validate: profile must be a customer of this tenant
        async with get_db_connection(use_transaction=False) as conn:
            customer_check = await conn.fetchrow(
                """
                SELECT COUNT(*) AS cnt
                FROM orders
                WHERE customer_id = $1 AND tenant_id = $2 AND status = 'completed'
                """,
                profile_id,
                tenant_id,
            )
            if int(customer_check["cnt"]) == 0:
                raise HTTPException(
                    status_code=422,
                    detail="El perfil no tiene órdenes completadas en este tenant",
                )

        # Atomic write
        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Lock + read current balance (0 if no wallet yet)
                wallet_row = await conn.fetchrow(
                    """
                    SELECT current_balance
                    FROM waros_wallets
                    WHERE profile_id = $1 AND tenant_id = $2
                    FOR UPDATE
                    """,
                    profile_id,
                    tenant_id,
                )
                current_balance = int(wallet_row["current_balance"]) if wallet_row else 0

                # 2. Guard: no negative balance
                if current_balance + waros_amount < 0:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Saldo insuficiente. Balance actual: {current_balance} Waros. "
                            f"No se pueden deducir {abs(waros_amount)} Waros."
                        ),
                    )

                # 3. Upsert wallet — lifetime_earned for positive, lifetime_spent for negative
                wallet_after = await conn.fetchrow(
                    """
                    INSERT INTO waros_wallets (
                        profile_id, tenant_id, current_balance, lifetime_earned,
                        lifetime_spent, daily_waros, weekly_waros, monthly_waros,
                        daily_reset_date, last_activity_date
                    )
                    VALUES (
                        $1, $2, $3,
                        GREATEST($3, 0), GREATEST(-$3, 0),
                        GREATEST($3, 0), GREATEST($3, 0), GREATEST($3, 0),
                        CURRENT_DATE, CURRENT_DATE
                    )
                    ON CONFLICT (profile_id, tenant_id) DO UPDATE SET
                        current_balance  = waros_wallets.current_balance + $3,
                        lifetime_earned  = waros_wallets.lifetime_earned + GREATEST($3, 0),
                        lifetime_spent   = waros_wallets.lifetime_spent  + GREATEST(-$3, 0),
                        last_activity_date = CURRENT_DATE,
                        updated_at       = now()
                    RETURNING current_balance
                    """,
                    profile_id,
                    tenant_id,
                    waros_amount,
                )
                new_balance = int(wallet_after["current_balance"])

                # 4. Audit log — get assignment ID for cross-reference
                assignment_row = await conn.fetchrow(
                    """
                    INSERT INTO waro_manual_assignments
                        (tenant_id, profile_id, waros_amount, reason, assigned_by)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    tenant_id,
                    profile_id,
                    waros_amount,
                    reason,
                    UUID(str(admin_user_id)),
                )
                assignment_id = assignment_row["id"]

                # 5. Transaction record
                description = reason if reason else (
                    "Asignación manual de Waros" if waros_amount > 0
                    else "Deducción manual de Waros"
                )
                tx_row = await conn.fetchrow(
                    """
                    INSERT INTO waros_transactions (
                        profile_id, tenant_id, transaction_type, waros_amount,
                        balance_after, description, related_entity_type, related_entity_id
                    )
                    VALUES ($1, $2, 'manual', $3, $4, $5, 'manual_assignment', $6)
                    RETURNING id
                    """,
                    profile_id,
                    tenant_id,
                    waros_amount,
                    new_balance,
                    description,
                    str(assignment_id),
                )

        logger.info(
            f"assign_manual_waros: {waros_amount:+d} waros → "
            f"profile={profile_id}, tenant={tenant_id}, "
            f"new_balance={new_balance}, by={admin_user_id}"
        )
        return {
            "assigned": True,
            "waros_amount": waros_amount,
            "new_balance": new_balance,
            "transaction_id": tx_row["id"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in assign_manual_waros: {e}")
        raise HTTPException(status_code=500, detail="Error al asignar Waros")


# ── Estimate (read-only) ─────────────────────────────────────────────────────

async def _estimate_waros_for_tenant(
    tenant_id: str,
    total_amount: float,
    customer_id: Optional[UUID],
) -> Dict[str, Any]:
    """Auth-agnostic core for waros estimate. Called by session wrapper and public API."""
    async with get_db_connection(use_transaction=False) as conn:
        # 1. Check system enabled + daily cap config
        config_row = await conn.fetchrow(
            """
            SELECT is_enabled, max_daily_waros
            FROM gamification_config
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
        if not config_row or not config_row["is_enabled"]:
            return {"estimated_waros": 0, "system_enabled": False, "breakdown": []}

        max_daily = int(config_row["max_daily_waros"] or 0)

        # 2. Fetch active rules
        rule_rows = await conn.fetch(
            """
            SELECT rule_type, config
            FROM waro_earning_rules
            WHERE tenant_id = $1 AND is_active = true
            """,
            tenant_id,
        )
        if not rule_rows:
            return {"estimated_waros": 0, "system_enabled": True, "breakdown": []}

        active_types = {r["rule_type"] for r in rule_rows}

        # 3. total_completed for purchase_count rule (+1: simulate this order completing)
        total_completed = 0
        if customer_id and "purchase_count" in active_types:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM orders
                WHERE customer_id = $1 AND tenant_id = $2 AND status = 'completed'
                """,
                customer_id,
                tenant_id,
            )
            total_completed = int(count_row["total"]) + 1

        # 4. freq_count for frequency rule (+1: simulate this order completing)
        freq_count = 0
        if customer_id and "frequency" in active_types:
            freq_cfg = next(
                (r["config"] for r in rule_rows if r["rule_type"] == "frequency"),
                {},
            )
            if isinstance(freq_cfg, str):
                freq_cfg = json.loads(freq_cfg)
            within_days = int(freq_cfg.get("within_days", 60))
            cutoff = datetime.now() - timedelta(days=within_days)
            freq_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS freq_count
                FROM orders
                WHERE customer_id = $1 AND tenant_id = $2
                  AND status = 'completed'
                  AND created_at >= $3
                """,
                customer_id,
                tenant_id,
                cutoff,
            )
            freq_count = int(freq_row["freq_count"]) + 1

        # 5. Today's earned waros for daily cap (only when customer_id provided)
        today_earned = 0
        if customer_id and max_daily > 0:
            today_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(waros_amount), 0) AS today_earned
                FROM waros_transactions
                WHERE profile_id = $1
                  AND tenant_id = $2
                  AND transaction_type = 'earned'
                  AND created_at >= CURRENT_DATE
                """,
                customer_id,
                tenant_id,
            )
            today_earned = int(today_row["today_earned"])

    # 6. Evaluate each active rule — total_qty=0 (unknown pre-order)
    breakdown: List[Dict[str, Any]] = []
    total_waros = 0

    for rule in rule_rows:
        rule_type = rule["rule_type"]
        config = rule["config"]
        if isinstance(config, str):
            config = json.loads(config)

        earned = _eval_rule(
            rule_type=rule_type,
            config=config,
            total_amount=total_amount,
            total_completed=total_completed,
            total_qty=0,
            freq_count=freq_count,
        )
        breakdown.append({"rule_type": rule_type, "waros": earned})
        total_waros += earned

    # 7. Apply daily cap (only when customer_id known)
    if customer_id and max_daily > 0 and total_waros > 0:
        remaining = max(0, max_daily - today_earned)
        total_waros = min(total_waros, remaining)

    return {
        "estimated_waros": total_waros,
        "system_enabled": True,
        "breakdown": breakdown,
    }



async def estimate_waros(
    request: Request,
    total_amount: float,
    customer_id: Optional[UUID],
) -> Dict[str, Any]:
    """
    GET /admin/waros/estimate
    Read-only preview of Waros that would be earned for an order with the given total.
    Never writes to DB. per_ticket_qty always returns 0 (item count unknown pre-order).
    purchase_count and frequency simulate +1 order (the one being estimated).
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        return await _estimate_waros_for_tenant(tenant_id, total_amount, customer_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in estimate_waros: {e}")
        raise HTTPException(status_code=500, detail="Error al estimar Waros")


# ── Rule evaluator ───────────────────────────────────────────────────────────

def _eval_rule(
    rule_type: str,
    config: Dict[str, Any],
    total_amount: float,
    total_completed: int,
    total_qty: int,
    freq_count: int,
) -> int:
    """
    Pure function: evaluate a single rule and return waros to award.
    Returns 0 if the rule does not fire.
    Python 3.9 safe — no match/case syntax.

    Supports both frontend config keys (base_waros/base_pesos/bonus/purchase_number)
    and legacy keys (base_waros_per_1000/bonus_waros/count) for backwards compatibility.
    """
    try:
        if rule_type == "ticket_value":
            # Frontend: base_waros + base_pesos. Legacy: base_waros_per_1000 (base_pesos=1000)
            base_waros = int(config.get("base_waros", config.get("base_waros_per_1000", 1)))
            base_pesos = int(config.get("base_pesos", 1000))
            if base_pesos <= 0:
                return 0
            units = int(total_amount // base_pesos)
            if units <= 0:
                return 0
            waros = units * base_waros
            # Apply highest matching tier multiplier
            # Frontend tiers use "from"/"to"/"multiplier"; legacy used "min_amount"
            tiers = config.get("tiers", [])
            if tiers:
                multiplier = 1.0
                for tier in sorted(tiers, key=lambda t: t.get("from", t.get("min_amount", 0))):
                    tier_from = float(tier.get("from", tier.get("min_amount", 0)))
                    if total_amount >= tier_from:
                        multiplier = float(tier.get("multiplier", 1.0))
                waros = int(waros * multiplier)
            return waros

        elif rule_type == "purchase_count":
            # Frontend: purchase_number + bonus. Legacy: count + bonus_waros
            for milestone in config.get("milestones", []):
                num = int(milestone.get("purchase_number", milestone.get("count", 0)))
                bonus = int(milestone.get("bonus", milestone.get("bonus_waros", 0)))
                if total_completed == num:
                    return bonus
            return 0

        elif rule_type == "frequency":
            required = int(config.get("purchases", 2))
            # Frontend: bonus. Legacy: bonus_waros
            bonus = int(config.get("bonus", config.get("bonus_waros", 75)))
            return bonus if freq_count >= required else 0

        elif rule_type == "per_ticket_qty":
            if total_qty <= 0:
                return 0
            # Frontend: points_per_item. Legacy: waros_per_ticket
            waros_per = int(config.get("points_per_item", config.get("waros_per_ticket", 10)))
            waros = total_qty * waros_per
            # Frontend bonus: bonus_from_qty + bonus_extra_points
            bonus_from_qty = config.get("bonus_from_qty")
            if bonus_from_qty and total_qty >= int(bonus_from_qty):
                waros += int(config.get("bonus_extra_points") or 0)
            # Legacy bonus: bonus_thresholds list
            for threshold in sorted(
                config.get("bonus_thresholds", []),
                key=lambda t: t.get("min_qty", 0),
            ):
                if total_qty >= int(threshold.get("min_qty", 0)):
                    waros += int(threshold.get("bonus_waros", 0))
            return waros

        return 0
    except Exception:
        return 0


async def evaluate_and_award(
    order_id: Any,
    customer_id: Any,
    tenant_id: Any,
) -> int:
    """
    Evaluate active waro_earning_rules for a completed order and award Waros.

    Called fire-and-forget via asyncio.create_task() — never raises.
    Returns total waros awarded (0 on any skip or error).
    """
    try:
        if customer_id is None:
            return 0

        async with get_db_connection(use_transaction=False) as conn:
            # 1. Check system enabled + daily cap
            config_row = await conn.fetchrow(
                """
                SELECT is_enabled, max_daily_waros
                FROM gamification_config
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            if not config_row or not config_row["is_enabled"]:
                return 0

            max_daily = int(config_row["max_daily_waros"] or 0)

            # 2. Fetch order total
            order_row = await conn.fetchrow(
                """
                SELECT total_amount
                FROM orders
                WHERE id = $1 AND tenant_id = $2
                """,
                order_id,
                tenant_id,
            )
            if not order_row:
                logger.warning(f"evaluate_and_award: order {order_id} not found")
                return 0

            total_amount = float(order_row["total_amount"])

            # 3. Fetch active rules
            rule_rows = await conn.fetch(
                """
                SELECT rule_type, config
                FROM waro_earning_rules
                WHERE tenant_id = $1 AND is_active = true
                """,
                tenant_id,
            )
            if not rule_rows:
                return 0

            active_types = {r["rule_type"] for r in rule_rows}

            # 4. Count completed orders for this customer (purchase_count + base for frequency)
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM orders
                WHERE customer_id = $1 AND tenant_id = $2 AND status = 'completed'
                """,
                customer_id,
                tenant_id,
            )
            total_completed = int(count_row["total"])

            # 5. Count orders in frequency window (only when frequency rule is active)
            freq_count = 0
            if "frequency" in active_types:
                freq_cfg = next(
                    (r["config"] for r in rule_rows if r["rule_type"] == "frequency"),
                    {},
                )
                if isinstance(freq_cfg, str):
                    freq_cfg = json.loads(freq_cfg)
                within_days = int(freq_cfg.get("within_days", 60))
                cutoff = datetime.now() - timedelta(days=within_days)
                freq_row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS freq_count
                    FROM orders
                    WHERE customer_id = $1 AND tenant_id = $2
                      AND status = 'completed'
                      AND created_at >= $3
                    """,
                    customer_id,
                    tenant_id,
                    cutoff,
                )
                freq_count = int(freq_row["freq_count"])

            # 6. Sum order item quantities (per_ticket_qty rule)
            qty_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(quantity), 0) AS total_qty
                FROM order_items
                WHERE order_id = $1
                """,
                order_id,
            )
            total_qty = int(qty_row["total_qty"])

            # 7. Today's earned waros (for daily cap)
            today_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(waros_amount), 0) AS today_earned
                FROM waros_transactions
                WHERE profile_id = $1
                  AND tenant_id = $2
                  AND transaction_type = 'earned'
                  AND created_at >= CURRENT_DATE
                """,
                customer_id,
                tenant_id,
            )
            today_earned = int(today_row["today_earned"])

        # 8. Evaluate each active rule
        rules_fired: List[Dict[str, Any]] = []
        total_waros = 0

        for rule in rule_rows:
            rule_type = rule["rule_type"]
            config = rule["config"]
            if isinstance(config, str):
                config = json.loads(config)

            earned = _eval_rule(
                rule_type=rule_type,
                config=config,
                total_amount=total_amount,
                total_completed=total_completed,
                total_qty=total_qty,
                freq_count=freq_count,
            )
            if earned > 0:
                rules_fired.append({"rule_type": rule_type, "waros": earned})
                total_waros += earned

        if total_waros == 0:
            return 0

        # 9. Apply daily cap
        if max_daily > 0:
            remaining = max(0, max_daily - today_earned)
            total_waros = min(total_waros, remaining)
            if total_waros == 0:
                logger.info(
                    f"evaluate_and_award: daily cap reached for customer={customer_id}"
                )
                return 0

        # 10. Atomic write: upsert wallet + insert transaction
        async with get_db_connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO waros_wallets (
                        profile_id, tenant_id, current_balance, lifetime_earned,
                        daily_waros, weekly_waros, monthly_waros,
                        daily_reset_date, last_activity_date
                    )
                    VALUES ($1, $2, $3, $3, $3, $3, $3, CURRENT_DATE, CURRENT_DATE)
                    ON CONFLICT (profile_id, tenant_id)
                    DO UPDATE SET
                        current_balance    = waros_wallets.current_balance + $3,
                        lifetime_earned    = waros_wallets.lifetime_earned + $3,
                        daily_waros        = CASE
                            WHEN waros_wallets.daily_reset_date < CURRENT_DATE
                            THEN $3
                            ELSE waros_wallets.daily_waros + $3
                        END,
                        daily_reset_date   = CURRENT_DATE,
                        weekly_waros       = waros_wallets.weekly_waros + $3,
                        monthly_waros      = waros_wallets.monthly_waros + $3,
                        last_activity_date = CURRENT_DATE,
                        updated_at         = now()
                    """,
                    customer_id,
                    tenant_id,
                    total_waros,
                )

                balance_row = await conn.fetchrow(
                    """
                    SELECT current_balance
                    FROM waros_wallets
                    WHERE profile_id = $1 AND tenant_id = $2
                    """,
                    customer_id,
                    tenant_id,
                )
                balance_after = (
                    int(balance_row["current_balance"])
                    if balance_row
                    else total_waros
                )

                await conn.execute(
                    """
                    INSERT INTO waros_transactions (
                        profile_id, tenant_id, transaction_type, waros_amount,
                        balance_after, description, related_entity_type,
                        related_entity_id, metadata
                    )
                    VALUES (
                        $1, $2, 'earned', $3, $4,
                        'Waros ganados por compra', 'order', $5, $6::jsonb
                    )
                    """,
                    customer_id,
                    tenant_id,
                    total_waros,
                    balance_after,
                    str(order_id),
                    json.dumps({"rules_fired": rules_fired}),
                )

        logger.info(
            f"evaluate_and_award: +{total_waros} waros → "
            f"customer={customer_id}, order={order_id}, "
            f"rules={[r['rule_type'] for r in rules_fired]}"
        )
        return total_waros

    except Exception as e:
        logger.error(
            f"evaluate_and_award error (order={order_id}, customer={customer_id}): {e}"
        )
        return 0


# ── Public API read functions (auth-agnostic) ────────────────────────────────

async def _get_customer_tx_history_for_tenant(
    tenant_id: str,
    profile_id: UUID,
    limit: int,
    offset: int,
    transaction_type: Optional[str],
) -> Dict[str, Any]:
    """
    Paginated WaRos transaction history for a customer, scoped to tenant.
    Called by session wrapper and public API.
    """
    async with get_db_connection(use_transaction=False) as conn:
        # Build optional type filter
        type_filter = "AND transaction_type = $5" if transaction_type else ""
        params_count: List[Any] = [profile_id, tenant_id, limit, offset]
        if transaction_type:
            params_count.append(transaction_type)

        count_params: List[Any] = [profile_id, tenant_id]
        count_filter = ""
        if transaction_type:
            count_filter = "AND transaction_type = $3"
            count_params.append(transaction_type)

        total_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS total
            FROM waros_transactions
            WHERE profile_id = $1 AND tenant_id = $2
            {count_filter}
            """,
            *count_params,
        )
        total_count = int(total_row["total"]) if total_row else 0

        rows = await conn.fetch(
            f"""
            SELECT id, transaction_type, waros_amount, balance_after,
                   description, related_entity_type, related_entity_id, created_at
            FROM waros_transactions
            WHERE profile_id = $1 AND tenant_id = $2
            {type_filter}
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
            """,
            *params_count,
        )

    transactions = [
        {
            "id": row["id"],
            "transaction_type": row["transaction_type"],
            "waros_amount": row["waros_amount"],
            "balance_after": row["balance_after"],
            "description": row["description"],
            "related_entity_type": row["related_entity_type"],
            "related_entity_id": row["related_entity_id"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]

    return {
        "profile_id": str(profile_id),
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "transactions": transactions,
    }


async def _get_waros_analytics_for_tenant(
    tenant_id: str,
    group_by: str,
    date_from: Optional[str],
    date_to: Optional[str],
) -> Dict[str, Any]:
    """
    Aggregate WaRos analytics for a tenant.
    group_by: 'customer' | 'day' | 'week'
    Called by session wrapper and public API.
    """
    from app.services.analytics_service import parse_date as _parse_date

    parsed_from = _parse_date(date_from) if date_from else None
    parsed_to = _parse_date(date_to) if date_to else None

    # Build date filter
    date_conditions: List[str] = []
    date_params: List[Any] = [tenant_id]

    if parsed_from:
        date_params.append(parsed_from)
        date_conditions.append(f"AND created_at >= ${len(date_params)}")
    if parsed_to:
        date_params.append(parsed_to)
        date_conditions.append(f"AND created_at < (${len(date_params)}::date + INTERVAL '1 day')")

    date_filter = " ".join(date_conditions)
    # Qualified version for queries that JOIN with other tables having created_at
    date_filter_wt = date_filter.replace("created_at", "wt.created_at")

    async with get_db_connection(use_transaction=False) as conn:
        # Summary row: totals across all transaction types
        summary_row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN transaction_type IN ('earned', 'manual') AND waros_amount > 0
                               THEN waros_amount ELSE 0 END), 0) AS total_issued,
                COALESCE(SUM(CASE WHEN transaction_type = 'redeemed'
                               THEN ABS(waros_amount) ELSE 0 END), 0) AS total_redeemed,
                COUNT(DISTINCT profile_id) AS active_members
            FROM waros_transactions
            WHERE tenant_id = $1
            {date_filter}
            """,
            *date_params,
        )

        total_issued = int(summary_row["total_issued"])
        total_redeemed = int(summary_row["total_redeemed"])
        active_members = int(summary_row["active_members"])
        redemption_rate = round(total_redeemed / total_issued * 100, 1) if total_issued > 0 else 0.0

        summary = {
            "total_issued": total_issued,
            "total_redeemed": total_redeemed,
            "redemption_rate_pct": redemption_rate,
            "active_members": active_members,
        }

        # Grouped rows
        if group_by == "customer":
            rows = await conn.fetch(
                f"""
                SELECT
                    wt.profile_id,
                    p.name,
                    p.phone_number,
                    COALESCE(SUM(CASE WHEN wt.transaction_type IN ('earned', 'manual') AND wt.waros_amount > 0
                                   THEN wt.waros_amount ELSE 0 END), 0) AS total_earned,
                    COALESCE(SUM(CASE WHEN wt.transaction_type = 'redeemed'
                                   THEN ABS(wt.waros_amount) ELSE 0 END), 0) AS total_redeemed,
                    COUNT(*) AS transaction_count
                FROM waros_transactions wt
                JOIN profile p ON p.id = wt.profile_id
                WHERE wt.tenant_id = $1
                  AND p.email NOT LIKE '%@customer.temp'
                {date_filter_wt}
                GROUP BY wt.profile_id, p.name, p.phone_number
                ORDER BY total_earned DESC
                LIMIT 100
                """,
                *date_params,
            )
            groups = [
                {
                    "profile_id": str(row["profile_id"]),
                    "name": row["name"],
                    "phone_number": row["phone_number"],
                    "total_earned": int(row["total_earned"]),
                    "total_redeemed": int(row["total_redeemed"]),
                    "transaction_count": int(row["transaction_count"]),
                }
                for row in rows
            ]

        elif group_by in ("day", "week"):
            trunc = "day" if group_by == "day" else "week"
            rows = await conn.fetch(
                f"""
                SELECT
                    date_trunc('{trunc}', created_at AT TIME ZONE 'America/Bogota') AS period,
                    COALESCE(SUM(CASE WHEN transaction_type IN ('earned', 'manual') AND waros_amount > 0
                                   THEN waros_amount ELSE 0 END), 0) AS total_earned,
                    COALESCE(SUM(CASE WHEN transaction_type = 'redeemed'
                                   THEN ABS(waros_amount) ELSE 0 END), 0) AS total_redeemed,
                    COUNT(DISTINCT profile_id) AS active_members
                FROM waros_transactions
                WHERE tenant_id = $1
                {date_filter}
                GROUP BY period
                ORDER BY period DESC
                """,
                *date_params,
            )
            groups = [
                {
                    "period": row["period"].date().isoformat(),
                    "total_earned": int(row["total_earned"]),
                    "total_redeemed": int(row["total_redeemed"]),
                    "active_members": int(row["active_members"]),
                }
                for row in rows
            ]

        else:
            groups = []

    return {
        "group_by": group_by,
        "summary": summary,
        "groups": groups,
    }
