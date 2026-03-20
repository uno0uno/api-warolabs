"""
Waros Points System Service
Business logic for configurable earning rules and manual assignments.
"""
import json
import logging
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
