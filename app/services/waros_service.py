"""
Waros Points System Service
Business logic for configurable earning rules and manual assignments.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

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
    """
    try:
        if rule_type == "ticket_value":
            base = int(config.get("base_waros_per_1000", 1))
            units = int(total_amount // 1000)
            if units <= 0:
                return 0
            waros = units * base
            # Apply highest matching tier multiplier
            tiers = config.get("tiers", [])
            if tiers:
                multiplier = 1.0
                for tier in sorted(tiers, key=lambda t: t.get("min_amount", 0)):
                    if total_amount >= float(tier.get("min_amount", 0)):
                        multiplier = float(tier.get("multiplier", 1.0))
                waros = int(waros * multiplier)
            return waros

        elif rule_type == "purchase_count":
            # Fire only when a milestone is reached exactly
            for milestone in config.get("milestones", []):
                if total_completed == int(milestone.get("count", 0)):
                    return int(milestone.get("bonus_waros", 0))
            return 0

        elif rule_type == "frequency":
            required = int(config.get("purchases", 2))
            bonus = int(config.get("bonus_waros", 75))
            return bonus if freq_count >= required else 0

        elif rule_type == "per_ticket_qty":
            if total_qty <= 0:
                return 0
            waros_per = int(config.get("waros_per_ticket", 10))
            waros = total_qty * waros_per
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
                    order_id,
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
