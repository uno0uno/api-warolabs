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
from app.core.timezones import resolve_tenant_timezone
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


# ── Redemption (checkout B1/B2/B3) — api#370 ─────────────────────────────────

REWARD_TYPES = {"fixed_cop_off", "free_product"}


async def _fetch_redemption_config_row(
    conn: Any,
    tenant_id: Any,
) -> Optional[Any]:
    return await conn.fetchrow(
        """
        SELECT is_enabled, redemption_enabled, waros_per_1000_cop,
               max_redeem_percent_per_order, min_waros_to_redeem,
               earn_on_wallet_payment, earn_base_excludes_waro_redemption
        FROM gamification_config
        WHERE tenant_id = $1
        """,
        tenant_id,
    )


def _b1_cop_from_waros(waros_amount: int, waros_per_1000_cop: int) -> int:
    if waros_amount <= 0 or waros_per_1000_cop <= 0:
        return 0
    return int(waros_amount * 1000 / waros_per_1000_cop)


async def _fetch_waro_reward_row(
    conn: Any,
    tenant_id: Any,
    reward_id: UUID,
) -> Optional[Any]:
    return await conn.fetchrow(
        """
        SELECT id, name, reward_type, waros_cost, fixed_cop_off, product_id, is_active
        FROM waro_rewards
        WHERE id = $1 AND tenant_id = $2
        """,
        reward_id,
        tenant_id,
    )


async def compute_redemption_preview(
    conn: Any,
    tenant_id: Any,
    customer_id: Optional[UUID],
    checkout_eval: Dict[str, Any],
    waros_to_redeem: Optional[int] = None,
    waro_reward_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    Read-only redemption math for preview and settlement validation.
    Layer: promos → manual → WaRo (this function).
    """
    from app.services.promotions_service import apply_waro_redemption_to_evaluated_lines

    config_row = await _fetch_redemption_config_row(conn, tenant_id)
    redemption_enabled = bool(config_row and config_row["redemption_enabled"])
    system_enabled = bool(config_row and config_row["is_enabled"])

    subtotal_after_promos = float(checkout_eval.get("subtotal_after_promos") or 0)
    manual_discount = float(checkout_eval.get("manual_discount_amount") or 0)
    base_after_manual = max(0.0, subtotal_after_promos - manual_discount)

    waros_per_1000 = int(config_row["waros_per_1000_cop"] or 100) if config_row else 100
    max_percent = float(config_row["max_redeem_percent_per_order"] or 50) if config_row else 50.0
    min_waros = int(config_row["min_waros_to_redeem"] or 1) if config_row else 1

    b1_waros = max(0, int(waros_to_redeem or 0))
    b2_waros = 0
    reward_fixed_off = 0.0
    free_product_id: Optional[UUID] = None
    reward_name: Optional[str] = None
    reward_type: Optional[str] = None

    if waro_reward_id:
        reward_row = await _fetch_waro_reward_row(conn, tenant_id, waro_reward_id)
        if not reward_row or not reward_row["is_active"]:
            raise HTTPException(status_code=422, detail="Recompensa WaRo no encontrada o inactiva")
        b2_waros = int(reward_row["waros_cost"])
        reward_name = reward_row["name"]
        reward_type = reward_row["reward_type"]
        if reward_type == "fixed_cop_off":
            reward_fixed_off = float(reward_row["fixed_cop_off"] or 0)
        elif reward_type == "free_product":
            free_product_id = reward_row["product_id"]

    base_canje = max(0.0, base_after_manual - reward_fixed_off)

    b1_cop_raw = 0
    b1_cop = 0
    if b1_waros > 0:
        if not redemption_enabled or not system_enabled:
            raise HTTPException(status_code=422, detail="Canje de WaRos no está habilitado")
        if b1_waros < min_waros:
            raise HTTPException(
                status_code=422,
                detail=f"Mínimo {min_waros} WaRos para canjear en esta orden",
            )
        b1_cop_raw = _b1_cop_from_waros(b1_waros, waros_per_1000)
        max_b1_cop = int(base_canje * max_percent / 100)
        b1_cop = min(b1_cop_raw, max_b1_cop, int(base_canje))

    if b2_waros > 0 and (not redemption_enabled or not system_enabled):
        raise HTTPException(status_code=422, detail="Canje de WaRos no está habilitado")

    total_waro_discount_cop = b1_cop + reward_fixed_off
    total_waros_cost = b1_waros + b2_waros

    wallet_balance = 0
    if customer_id and total_waros_cost > 0:
        bal_row = await conn.fetchrow(
            """
            SELECT current_balance FROM waros_wallets
            WHERE profile_id = $1 AND tenant_id = $2
            """,
            customer_id,
            tenant_id,
        )
        wallet_balance = int(bal_row["current_balance"]) if bal_row else 0
        if wallet_balance < total_waros_cost:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Saldo WaRo insuficiente. Disponible: {wallet_balance}, "
                    f"requerido: {total_waros_cost}"
                ),
            )

    updated_eval = apply_waro_redemption_to_evaluated_lines(checkout_eval, total_waro_discount_cop)

    return {
        "redemption_enabled": redemption_enabled and system_enabled,
        "subtotal_after_promos": round(subtotal_after_promos),
        "manual_discount_amount": round(manual_discount),
        "base_after_manual": round(base_after_manual),
        "base_canje": round(base_canje),
        "b1_waros": b1_waros,
        "b1_cop": b1_cop,
        "b1_cop_raw": b1_cop_raw,
        "b2_waros": b2_waros,
        "reward_fixed_off": round(reward_fixed_off),
        "reward_type": reward_type,
        "reward_name": reward_name,
        "free_product_id": str(free_product_id) if free_product_id else None,
        "waro_reward_id": str(waro_reward_id) if waro_reward_id else None,
        "total_waro_discount_cop": round(total_waro_discount_cop),
        "total_waros_cost": total_waros_cost,
        "max_redeem_percent": max_percent,
        "max_b1_cop_cap": int(base_canje * max_percent / 100),
        "wallet_balance": wallet_balance,
        "total_after_redemption": updated_eval["total_amount"],
        "checkout_eval": updated_eval,
    }


async def settle_waro_redemption(
    conn: Any,
    tenant_id: Any,
    customer_id: UUID,
    order_id: Any,
    preview: Dict[str, Any],
) -> None:
    """Atomic WaRo wallet debit + detail rows inside caller transaction."""
    total_waros = int(preview.get("total_waros_cost") or 0)
    if total_waros <= 0:
        return

    total_cop = float(preview.get("total_waro_discount_cop") or 0)

    wallet_row = await conn.fetchrow(
        """
        SELECT current_balance
        FROM waros_wallets
        WHERE profile_id = $1 AND tenant_id = $2
        FOR UPDATE
        """,
        customer_id,
        tenant_id,
    )
    current_balance = int(wallet_row["current_balance"]) if wallet_row else 0
    if current_balance < total_waros:
        raise HTTPException(
            status_code=422,
            detail=f"Saldo WaRo insuficiente ({current_balance} < {total_waros})",
        )

    if wallet_row:
        await conn.execute(
            """
            UPDATE waros_wallets SET
                current_balance = current_balance - $3,
                lifetime_spent = lifetime_spent + $3,
                last_activity_date = CURRENT_DATE,
                updated_at = now()
            WHERE profile_id = $1 AND tenant_id = $2
            """,
            customer_id,
            tenant_id,
            total_waros,
        )
        new_balance = current_balance - total_waros
    else:
        raise HTTPException(status_code=422, detail="Cliente sin billetera WaRo")

    await conn.execute(
        """
        INSERT INTO waros_transactions (
            profile_id, tenant_id, transaction_type, waros_amount,
            balance_after, description, related_entity_type, related_entity_id
        )
        VALUES ($1, $2, 'redeemed', $3, $4, $5, 'order', $6)
        """,
        customer_id,
        tenant_id,
        -total_waros,
        new_balance,
        f"WaRos canjeados en orden ({total_waros} pts, -${int(total_cop)} COP)",
        str(order_id),
    )

    await conn.execute(
        """
        UPDATE orders
        SET waros_redeemed = $2, waro_redeemed_amount_cop = $3
        WHERE id = $1
        """,
        order_id,
        total_waros,
        total_cop,
    )

    b1_waros = int(preview.get("b1_waros") or 0)
    b1_cop = float(preview.get("b1_cop") or 0)
    if b1_waros > 0:
        await conn.execute(
            """
            INSERT INTO order_waro_redemptions (
                order_id, tenant_id, redemption_type, waros_spent, cop_discount
            )
            VALUES ($1, $2, 'points_cop', $3, $4)
            """,
            order_id,
            tenant_id,
            b1_waros,
            b1_cop,
        )

    b2_waros = int(preview.get("b2_waros") or 0)
    reward_type = preview.get("reward_type")
    waro_reward_id = preview.get("waro_reward_id")
    reward_uuid = UUID(waro_reward_id) if waro_reward_id else None

    if b2_waros > 0 and reward_type == "fixed_cop_off":
        await conn.execute(
            """
            INSERT INTO order_waro_redemptions (
                order_id, tenant_id, redemption_type, waros_spent, cop_discount, waro_reward_id
            )
            VALUES ($1, $2, 'reward_fixed_cop', $3, $4, $5)
            """,
            order_id,
            tenant_id,
            b2_waros,
            float(preview.get("reward_fixed_off") or 0),
            reward_uuid,
        )
    elif b2_waros > 0 and reward_type == "free_product":
        free_product_id = preview.get("free_product_id")
        order_item_id = None
        if free_product_id:
            item_row = await conn.fetchrow(
                """
                INSERT INTO order_items (
                    order_id, product_id, quantity, price_at_purchase, subtotal,
                    discount_allocated, net_total, promo_opt_out, line_source
                )
                VALUES ($1, $2::uuid, 1, 0, 0, 0, 0, true, 'waro_reward')
                RETURNING id
                """,
                order_id,
                free_product_id,
            )
            order_item_id = item_row["id"] if item_row else None
        await conn.execute(
            """
            INSERT INTO order_waro_redemptions (
                order_id, tenant_id, redemption_type, waros_spent, cop_discount,
                waro_reward_id, order_item_id
            )
            VALUES ($1, $2, 'reward_free_product', $3, 0, $4, $5)
            """,
            order_id,
            tenant_id,
            b2_waros,
            reward_uuid,
            order_item_id,
        )


async def apply_checkout_waro_redemption(
    conn: Any,
    tenant_id: Any,
    customer_id: Optional[UUID],
    checkout_eval: Dict[str, Any],
    waros_to_redeem: Optional[int] = None,
    waro_reward_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """Preview + apply layer 4 to checkout_eval. No wallet write."""
    if not waros_to_redeem and not waro_reward_id:
        return checkout_eval
    if not customer_id:
        raise HTTPException(
            status_code=422,
            detail="Canje WaRo requiere un cliente identificado",
        )
    preview = await compute_redemption_preview(
        conn,
        tenant_id,
        customer_id,
        checkout_eval,
        waros_to_redeem=waros_to_redeem,
        waro_reward_id=waro_reward_id,
    )
    checkout_eval["_waro_redemption_preview"] = preview
    return preview["checkout_eval"]


async def preview_redemption(
    request: Request,
    lines: List[Dict[str, Any]],
    customer_id: Optional[UUID] = None,
    manual_discount_amount: float = 0,
    discount_type: Optional[str] = None,
    discount_value: Optional[float] = None,
    waros_to_redeem: Optional[int] = None,
    waro_reward_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """GET /admin/waros/preview-redemption service entry."""
    from app.services.promotions_service import evaluate_checkout_promotions

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=False) as conn:
        checkout_eval = await evaluate_checkout_promotions(
            conn,
            UUID(str(tenant_id)),
            lines,
            manual_discount_amount=manual_discount_amount,
            discount_type=discount_type,
            discount_value=discount_value,
        )
        preview = await compute_redemption_preview(
            conn,
            tenant_id,
            customer_id,
            checkout_eval,
            waros_to_redeem=waros_to_redeem,
            waro_reward_id=waro_reward_id,
        )
    preview.pop("checkout_eval", None)
    return preview


async def list_waro_rewards(request: Request) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, reward_type, waros_cost, fixed_cop_off, product_id, is_active,
                   created_at, updated_at
            FROM waro_rewards
            WHERE tenant_id = $1
            ORDER BY name
            """,
            tenant_id,
        )
    rewards = [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "reward_type": r["reward_type"],
            "waros_cost": int(r["waros_cost"]),
            "fixed_cop_off": float(r["fixed_cop_off"]) if r["fixed_cop_off"] is not None else None,
            "product_id": str(r["product_id"]) if r["product_id"] else None,
            "is_active": r["is_active"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]
    return {"rewards": rewards}


async def create_waro_reward(
    request: Request,
    name: str,
    reward_type: str,
    waros_cost: int,
    fixed_cop_off: Optional[float] = None,
    product_id: Optional[UUID] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    if reward_type not in REWARD_TYPES:
        raise HTTPException(status_code=422, detail=f"reward_type inválido: {sorted(REWARD_TYPES)}")
    if waros_cost <= 0:
        raise HTTPException(status_code=422, detail="waros_cost debe ser positivo")
    if reward_type == "fixed_cop_off" and (fixed_cop_off is None or fixed_cop_off <= 0):
        raise HTTPException(status_code=422, detail="fixed_cop_off requerido para fixed_cop_off")
    if reward_type == "free_product" and product_id is None:
        raise HTTPException(status_code=422, detail="product_id requerido para free_product")

    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO waro_rewards (
                tenant_id, name, reward_type, waros_cost, fixed_cop_off, product_id, is_active
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, name, reward_type, waros_cost, fixed_cop_off, product_id, is_active
            """,
            tenant_id,
            name,
            reward_type,
            waros_cost,
            fixed_cop_off,
            product_id,
            is_active,
        )
    return {"reward": dict(row)}


async def update_waro_reward(
    request: Request,
    reward_id: UUID,
    name: Optional[str] = None,
    waros_cost: Optional[int] = None,
    fixed_cop_off: Optional[float] = None,
    product_id: Optional[UUID] = None,
    is_active: Optional[bool] = None,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection() as conn:
        existing = await _fetch_waro_reward_row(conn, tenant_id, reward_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Recompensa no encontrada")
        row = await conn.fetchrow(
            """
            UPDATE waro_rewards SET
                name = COALESCE($3, name),
                waros_cost = COALESCE($4, waros_cost),
                fixed_cop_off = COALESCE($5, fixed_cop_off),
                product_id = COALESCE($6, product_id),
                is_active = COALESCE($7, is_active),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, name, reward_type, waros_cost, fixed_cop_off, product_id, is_active
            """,
            reward_id,
            tenant_id,
            name,
            waros_cost,
            fixed_cop_off,
            product_id,
            is_active,
        )
    return {"reward": dict(row)}


async def delete_waro_reward(request: Request, reward_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection() as conn:
        result = await conn.execute(
            "DELETE FROM waro_rewards WHERE id = $1 AND tenant_id = $2",
            reward_id,
            tenant_id,
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Recompensa no encontrada")
    return {"deleted": True, "id": str(reward_id)}


async def update_redemption_config(
    request: Request,
    redemption_enabled: Optional[bool] = None,
    waros_per_1000_cop: Optional[int] = None,
    max_redeem_percent_per_order: Optional[float] = None,
    min_waros_to_redeem: Optional[int] = None,
    earn_on_wallet_payment: Optional[bool] = None,
    earn_base_excludes_waro_redemption: Optional[bool] = None,
    is_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO gamification_config (
                tenant_id, is_enabled, redemption_enabled, waros_per_1000_cop,
                max_redeem_percent_per_order, min_waros_to_redeem,
                earn_on_wallet_payment, earn_base_excludes_waro_redemption
            )
            VALUES (
                $1,
                COALESCE($8, true),
                COALESCE($2, false),
                COALESCE($3, 100),
                COALESCE($4, 50),
                COALESCE($5, 1),
                COALESCE($6, false),
                COALESCE($7, true)
            )
            ON CONFLICT (tenant_id) DO UPDATE SET
                is_enabled = COALESCE($8, gamification_config.is_enabled),
                redemption_enabled = COALESCE($2, gamification_config.redemption_enabled),
                waros_per_1000_cop = COALESCE($3, gamification_config.waros_per_1000_cop),
                max_redeem_percent_per_order = COALESCE(
                    $4, gamification_config.max_redeem_percent_per_order
                ),
                min_waros_to_redeem = COALESCE($5, gamification_config.min_waros_to_redeem),
                earn_on_wallet_payment = COALESCE($6, gamification_config.earn_on_wallet_payment),
                earn_base_excludes_waro_redemption = COALESCE(
                    $7, gamification_config.earn_base_excludes_waro_redemption
                ),
                updated_at = now()
            RETURNING tenant_id, is_enabled, redemption_enabled, waros_per_1000_cop,
                      max_redeem_percent_per_order, min_waros_to_redeem,
                      earn_on_wallet_payment, earn_base_excludes_waro_redemption
            """,
            tenant_id,
            redemption_enabled,
            waros_per_1000_cop,
            max_redeem_percent_per_order,
            min_waros_to_redeem,
            earn_on_wallet_payment,
            earn_base_excludes_waro_redemption,
            is_enabled,
        )
    return {
        "tenant_id": str(row["tenant_id"]),
        "is_enabled": row["is_enabled"],
        "redemption_enabled": row["redemption_enabled"],
        "waros_per_1000_cop": int(row["waros_per_1000_cop"]),
        "max_redeem_percent_per_order": float(row["max_redeem_percent_per_order"]),
        "min_waros_to_redeem": int(row["min_waros_to_redeem"]),
        "earn_on_wallet_payment": row["earn_on_wallet_payment"],
        "earn_base_excludes_waro_redemption": row["earn_base_excludes_waro_redemption"],
    }


async def get_redemption_config(request: Request) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    async with get_db_connection(use_transaction=False) as conn:
        row = await _fetch_redemption_config_row(conn, tenant_id)
    if not row:
        return {
            "is_enabled": False,
            "redemption_enabled": False,
            "waros_per_1000_cop": 100,
            "max_redeem_percent_per_order": 50.0,
            "min_waros_to_redeem": 1,
            "earn_on_wallet_payment": False,
            "earn_base_excludes_waro_redemption": True,
        }
    return {
        "is_enabled": row["is_enabled"],
        "redemption_enabled": row["redemption_enabled"],
        "waros_per_1000_cop": int(row["waros_per_1000_cop"]),
        "max_redeem_percent_per_order": float(row["max_redeem_percent_per_order"]),
        "min_waros_to_redeem": int(row["min_waros_to_redeem"]),
        "earn_on_wallet_payment": row["earn_on_wallet_payment"],
        "earn_base_excludes_waro_redemption": row["earn_base_excludes_waro_redemption"],
    }


# ── Estimate (read-only) ─────────────────────────────────────────────────────

async def _estimate_waros_for_tenant(
    tenant_id: str,
    total_amount: float,
    customer_id: Optional[UUID],
    payment_method: Optional[str] = None,
) -> Dict[str, Any]:
    """Auth-agnostic core for waros estimate. Called by session wrapper and public API."""
    async with get_db_connection(use_transaction=False) as conn:
        # 1. Check system enabled + daily cap config
        config_row = await conn.fetchrow(
            """
            SELECT is_enabled, max_daily_waros, earn_on_wallet_payment
            FROM gamification_config
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
        if not config_row or not config_row["is_enabled"]:
            return {
                "estimated_waros": 0,
                "system_enabled": False,
                "breakdown": [],
                "earn_eligible": False,
            }

        if payment_method == "customer_wallet" and not config_row["earn_on_wallet_payment"]:
            return {
                "estimated_waros": 0,
                "system_enabled": True,
                "breakdown": [],
                "earn_eligible": False,
                "earn_block_reason": "wallet_payment",
            }

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
            return {
                "estimated_waros": 0,
                "system_enabled": True,
                "breakdown": [],
                "earn_eligible": True,
            }

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
        "earn_eligible": True,
    }



async def estimate_waros(
    request: Request,
    total_amount: float,
    customer_id: Optional[UUID],
    payment_method: Optional[str] = None,
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
        return await _estimate_waros_for_tenant(tenant_id, total_amount, customer_id, payment_method)
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
                SELECT is_enabled, max_daily_waros,
                       earn_on_wallet_payment, earn_base_excludes_waro_redemption
                FROM gamification_config
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            if not config_row or not config_row["is_enabled"]:
                return 0

            max_daily = int(config_row["max_daily_waros"] or 0)
            earn_on_wallet = bool(config_row["earn_on_wallet_payment"])
            exclude_waro_redemption = bool(config_row["earn_base_excludes_waro_redemption"])

            # 2. Fetch order totals for eligible earn base
            order_row = await conn.fetchrow(
                """
                SELECT status, total_amount, waro_redeemed_amount_cop, payment_method
                FROM orders
                WHERE id = $1 AND tenant_id = $2
                """,
                order_id,
                tenant_id,
            )
            if not order_row:
                logger.warning(f"evaluate_and_award: order {order_id} not found")
                return 0
            if order_row["status"] != "completed":
                return 0

            total_amount = float(order_row["total_amount"])
            waro_redeemed_cop = float(order_row["waro_redeemed_amount_cop"] or 0)

            eligible_amount = total_amount
            if exclude_waro_redemption and waro_redeemed_cop > 0:
                eligible_amount += waro_redeemed_cop

            if not earn_on_wallet and order_row["payment_method"] == "customer_wallet":
                eligible_amount = 0.0
            elif not earn_on_wallet:
                wallet_paid = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM order_payments
                    WHERE order_id = $1 AND payment_method = 'customer_wallet'
                    """,
                    order_id,
                )
                if wallet_paid:
                    eligible_amount = max(0.0, eligible_amount - float(wallet_paid))

            total_amount = eligible_amount
            if total_amount <= 0:
                return 0

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


async def revoke_waros_awarded_for_order(
    conn,
    order_id: Any,
    tenant_id: Any,
) -> int:
    """Remove Waros earned for this order. Idempotent. Clamps at zero balance."""
    rows = await conn.fetch(
        """
        SELECT
            profile_id,
            COALESCE(SUM(CASE WHEN waros_amount > 0 THEN waros_amount ELSE 0 END), 0)
              - COALESCE(SUM(CASE WHEN waros_amount < 0 THEN -waros_amount ELSE 0 END), 0)
              AS net_awarded
        FROM waros_transactions
        WHERE tenant_id = $1
          AND related_entity_type = 'order'
          AND related_entity_id = $2
          AND transaction_type = 'earned'
        GROUP BY profile_id
        """,
        tenant_id,
        str(order_id),
    )
    if not isinstance(rows, (list, tuple)):
        return 0

    total_revoked = 0
    for row in rows:
        try:
            profile_id = row["profile_id"]
            net_awarded = int(row["net_awarded"] or 0)
        except (KeyError, TypeError):
            continue
        if not profile_id or net_awarded <= 0:
            continue

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
        deduct = min(net_awarded, max(current_balance, 0))
        if deduct <= 0:
            continue

        new_balance = current_balance - deduct
        await conn.execute(
            """
            UPDATE waros_wallets SET
                current_balance = current_balance - $3,
                lifetime_spent = lifetime_spent + $3,
                last_activity_date = CURRENT_DATE,
                updated_at = now()
            WHERE profile_id = $1 AND tenant_id = $2
            """,
            profile_id,
            tenant_id,
            deduct,
        )
        await conn.execute(
            """
            INSERT INTO waros_transactions (
                profile_id, tenant_id, transaction_type, waros_amount,
                balance_after, description, related_entity_type, related_entity_id
            )
            VALUES (
                $1, $2, 'earned', $3, $4,
                'Reverso Waros por cancelación de venta', 'order', $5
            )
            """,
            profile_id,
            tenant_id,
            -deduct,
            new_balance,
            str(order_id),
        )
        total_revoked += deduct

    return total_revoked


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

    async with get_db_connection(use_transaction=False) as conn:
        # Operational tenant TZ for filters + day/week buckets (not fiscal Colombia time).
        timezone_name = await resolve_tenant_timezone(conn, tenant_id)

        # $1 = tenant. When date range is set, bind timezone next so filters use
        # DATE(created_at AT TIME ZONE $tz) (tenant-local calendar, not session TZ).
        date_params: List[Any] = [tenant_id]
        date_conditions: List[str] = []
        filter_tz_param: Optional[int] = None

        if parsed_from or parsed_to:
            date_params.append(timezone_name)
            filter_tz_param = len(date_params)
            if parsed_from:
                date_params.append(parsed_from)
                date_conditions.append(
                    f"AND DATE(created_at AT TIME ZONE ${filter_tz_param}) >= ${len(date_params)}"
                )
            if parsed_to:
                date_params.append(parsed_to)
                date_conditions.append(
                    f"AND DATE(created_at AT TIME ZONE ${filter_tz_param}) <= ${len(date_params)}"
                )

        date_filter = " ".join(date_conditions)
        # Qualified version for queries that JOIN with other tables having created_at
        date_filter_wt = date_filter.replace("created_at", "wt.created_at")

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
            # Buckets share the filter timezone bind when present; else append $2.
            if filter_tz_param is not None:
                bucket_params = list(date_params)
                bucket_tz_param = filter_tz_param
            else:
                bucket_params = [tenant_id, timezone_name]
                bucket_tz_param = 2
            rows = await conn.fetch(
                f"""
                SELECT
                    date_trunc('{trunc}', created_at AT TIME ZONE ${bucket_tz_param}) AS period,
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
                *bucket_params,
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
