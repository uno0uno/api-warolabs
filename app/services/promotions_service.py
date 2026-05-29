"""Tenant promotion CRUD and schedule evaluation (warocol.com#980)."""
import json
import logging
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Sequence, Set
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException, Request

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.tenant_promotion import (
    PromotionCreate,
    PromotionUpdate,
    ScopeType,
)

logger = logging.getLogger(__name__)

BOGOTA = ZoneInfo("America/Bogota")

# v1 contract: higher priority wins; default non-stackable (documented on router).
CONFLICT_RULES_DOC = (
    "When multiple promotions match a line, the highest priority wins. "
    "stackable=false (default) means one promotion per line. "
    "Manual order-level discounts are applied separately in checkout (batch #982)."
)


def day_bit_for_datetime(at: datetime) -> int:
    """Monday=1<<0 … Sunday=1<<6 in America/Bogota."""
    local = at.astimezone(BOGOTA)
    return 1 << local.weekday()


def time_in_schedule_window(
    at: datetime,
    *,
    days_of_week: int,
    start_time: time,
    end_time: time,
    crosses_midnight: bool,
) -> bool:
    local = at.astimezone(BOGOTA)
    current = local.time()
    today_bit = day_bit_for_datetime(local)
    if crosses_midnight:
        if (days_of_week & today_bit) and current >= start_time:
            return True
        yesterday_bit = 1 << ((local.weekday() - 1) % 7)
        if (days_of_week & yesterday_bit) and current < end_time:
            return True
        return False
    if not (days_of_week & today_bit):
        return False
    return start_time <= current < end_time


def is_active_at(
    at: datetime,
    *,
    is_active: bool,
    starts_at: Optional[datetime],
    ends_at: Optional[datetime],
    schedules: Sequence[Dict[str, Any]],
) -> bool:
    """Return whether a promotion rule is active at `at` (evaluated in Bogotá)."""
    if not is_active:
        return False
    if starts_at is not None and at < starts_at:
        return False
    if ends_at is not None and at >= ends_at:
        return False
    if not schedules:
        return True
    for row in schedules:
        if time_in_schedule_window(
            at,
            days_of_week=int(row["days_of_week"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            crosses_midnight=bool(row["crosses_midnight"]),
        ):
            return True
    return False


def product_in_scope(
    *,
    scope_type: str,
    category_ids: Set[UUID],
    product_ids: Set[UUID],
    product_id: UUID,
    category_id: Optional[UUID],
) -> bool:
    if scope_type == ScopeType.ALL_PRODUCTS.value:
        return True
    if scope_type == ScopeType.PRODUCTS.value:
        return product_id in product_ids
    if scope_type == ScopeType.CATEGORIES.value:
        return category_id is not None and category_id in category_ids
    return False


def _parse_value_json(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


def _row_to_schedule(row: asyncpg.Record) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "days_of_week": row["days_of_week"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "crosses_midnight": row["crosses_midnight"],
        "sort_order": row["sort_order"],
    }


def _serialize_promotion(
    promo_row: asyncpg.Record,
    schedules: List[asyncpg.Record],
    category_ids: List[UUID],
    product_ids: List[UUID],
    *,
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    schedule_dicts = [_row_to_schedule(r) for r in schedules]
    payload: Dict[str, Any] = {
        "id": str(promo_row["id"]),
        "tenant_id": str(promo_row["tenant_id"]),
        "name": promo_row["name"],
        "promo_type": promo_row["promo_type"],
        "value_json": _parse_value_json(promo_row["value_json"]),
        "scope_type": promo_row["scope_type"],
        "category_ids": [str(cid) for cid in category_ids],
        "product_ids": [str(pid) for pid in product_ids],
        "schedules": [
            {
                "id": str(s["id"]),
                "days_of_week": s["days_of_week"],
                "start_time": s["start_time"].isoformat(),
                "end_time": s["end_time"].isoformat(),
                "crosses_midnight": s["crosses_midnight"],
                "sort_order": s["sort_order"],
            }
            for s in schedule_dicts
        ],
        "priority": promo_row["priority"],
        "is_active": promo_row["is_active"],
        "stackable": promo_row["stackable"],
        "starts_at": promo_row["starts_at"].isoformat() if promo_row["starts_at"] else None,
        "ends_at": promo_row["ends_at"].isoformat() if promo_row["ends_at"] else None,
        "created_at": promo_row["created_at"].isoformat(),
        "updated_at": promo_row["updated_at"].isoformat(),
    }
    if at is not None:
        payload["is_currently_active"] = is_active_at(
            at,
            is_active=promo_row["is_active"],
            starts_at=promo_row["starts_at"],
            ends_at=promo_row["ends_at"],
            schedules=schedule_dicts,
        )
    return payload


async def _load_scope_ids(
    conn: asyncpg.Connection, promotion_id: UUID
) -> tuple[List[UUID], List[UUID]]:
    cat_rows = await conn.fetch(
        """
        SELECT category_id FROM tenant_promotion_scope_categories
        WHERE promotion_id = $1
        """,
        promotion_id,
    )
    prod_rows = await conn.fetch(
        """
        SELECT product_id FROM tenant_promotion_scope_products
        WHERE promotion_id = $1
        """,
        promotion_id,
    )
    return [r["category_id"] for r in cat_rows], [r["product_id"] for r in prod_rows]


async def _load_schedules(conn: asyncpg.Connection, promotion_id: UUID) -> List[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, days_of_week, start_time, end_time, crosses_midnight, sort_order
        FROM tenant_promotion_schedules
        WHERE promotion_id = $1
        ORDER BY sort_order, start_time
        """,
        promotion_id,
    )


async def _replace_scope(
    conn: asyncpg.Connection,
    promotion_id: UUID,
    tenant_id: UUID,
    scope_type: str,
    category_ids: List[UUID],
    product_ids: List[UUID],
) -> None:
    await conn.execute(
        "DELETE FROM tenant_promotion_scope_categories WHERE promotion_id = $1",
        promotion_id,
    )
    await conn.execute(
        "DELETE FROM tenant_promotion_scope_products WHERE promotion_id = $1",
        promotion_id,
    )
    if scope_type == ScopeType.CATEGORIES.value:
        for category_id in category_ids:
            owned = await conn.fetchval(
                """
                SELECT 1 FROM categories
                WHERE id = $1 AND tenant_id = $2
                """,
                category_id,
                tenant_id,
            )
            if not owned:
                raise HTTPException(status_code=400, detail=f"Category {category_id} not found")
            await conn.execute(
                """
                INSERT INTO tenant_promotion_scope_categories (promotion_id, category_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                promotion_id,
                category_id,
            )
    elif scope_type == ScopeType.PRODUCTS.value:
        for product_id in product_ids:
            owned = await conn.fetchval(
                """
                SELECT 1 FROM product
                WHERE id = $1 AND tenant_id = $2
                """,
                product_id,
                tenant_id,
            )
            if not owned:
                raise HTTPException(status_code=400, detail=f"Product {product_id} not found")
            await conn.execute(
                """
                INSERT INTO tenant_promotion_scope_products (promotion_id, product_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                promotion_id,
                product_id,
            )


async def _replace_schedules(
    conn: asyncpg.Connection,
    promotion_id: UUID,
    schedules: Sequence[Any],
) -> None:
    await conn.execute(
        "DELETE FROM tenant_promotion_schedules WHERE promotion_id = $1",
        promotion_id,
    )
    for sched in schedules:
        await conn.execute(
            """
            INSERT INTO tenant_promotion_schedules (
                promotion_id, days_of_week, start_time, end_time,
                crosses_midnight, sort_order
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            promotion_id,
            sched.days_of_week,
            sched.start_time,
            sched.end_time,
            sched.crosses_midnight,
            sched.sort_order,
        )


async def _get_owned_promotion(conn: asyncpg.Connection, promotion_id: UUID, tenant_id: UUID):
    row = await conn.fetchrow(
        "SELECT * FROM tenant_promotions WHERE id = $1 AND tenant_id = $2",
        promotion_id,
        tenant_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Promotion not found")
    return row


async def list_promotions(
    request: Request,
    *,
    include_inactive: bool = False,
    at: Optional[datetime] = None,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    query = """
        SELECT * FROM tenant_promotions
        WHERE tenant_id = $1
    """
    if not include_inactive:
        query += " AND is_active = true"
    query += " ORDER BY priority DESC, name"

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(query, tenant_id)
        data = []
        for row in rows:
            schedules = await _load_schedules(conn, row["id"])
            category_ids, product_ids = await _load_scope_ids(conn, row["id"])
            data.append(
                _serialize_promotion(
                    row, schedules, category_ids, product_ids, at=at
                )
            )

    return {"success": True, "total": len(data), "data": data}


async def get_promotion(
    request: Request,
    promotion_id: UUID,
    *,
    at: Optional[datetime] = None,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=False) as conn:
        row = await _get_owned_promotion(conn, promotion_id, tenant_id)
        schedules = await _load_schedules(conn, promotion_id)
        category_ids, product_ids = await _load_scope_ids(conn, promotion_id)

    return {
        "success": True,
        "data": _serialize_promotion(
            row, schedules, category_ids, product_ids, at=at
        ),
    }


async def create_promotion(request: Request, body: PromotionCreate) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    try:
        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_promotions (
                    tenant_id, name, promo_type, value_json, scope_type,
                    priority, is_active, stackable, starts_at, ends_at
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                tenant_id,
                body.name.strip(),
                body.promo_type.value,
                json.dumps(body.value_json),
                body.scope_type.value,
                body.priority,
                body.is_active,
                body.stackable,
                body.starts_at,
                body.ends_at,
            )
            promotion_id = row["id"]
            await _replace_schedules(conn, promotion_id, body.schedules)
            await _replace_scope(
                conn,
                promotion_id,
                tenant_id,
                body.scope_type.value,
                body.category_ids,
                body.product_ids,
            )
            schedules = await _load_schedules(conn, promotion_id)
            category_ids, product_ids = await _load_scope_ids(conn, promotion_id)
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una promoción con ese nombre",
        ) from None

    logger.info("Created promotion %r for tenant %s", body.name, tenant_id)
    return {
        "success": True,
        "data": _serialize_promotion(row, schedules, category_ids, product_ids),
    }


async def update_promotion(
    request: Request,
    promotion_id: UUID,
    body: PromotionUpdate,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    schedules = data.pop("schedules", None)
    category_ids = data.pop("category_ids", None)
    product_ids = data.pop("product_ids", None)

    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()
    if "promo_type" in data and data["promo_type"] is not None:
        data["promo_type"] = data["promo_type"].value
    if "scope_type" in data and data["scope_type"] is not None:
        data["scope_type"] = data["scope_type"].value
    if "value_json" in data and data["value_json"] is not None:
        data["value_json"] = json.dumps(data["value_json"])

    async with get_db_connection() as conn:
        existing = await _get_owned_promotion(conn, promotion_id, tenant_id)

        if data:
            set_parts = []
            params: List[Any] = []
            idx = 1
            for key, value in data.items():
                if key == "value_json":
                    set_parts.append(f"value_json = ${idx}::jsonb")
                else:
                    set_parts.append(f"{key} = ${idx}")
                params.append(value)
                idx += 1
            set_parts.append("updated_at = NOW()")
            params.extend([promotion_id, tenant_id])
            row = await conn.fetchrow(
                f"""
                UPDATE tenant_promotions
                SET {", ".join(set_parts)}
                WHERE id = ${idx} AND tenant_id = ${idx + 1}
                RETURNING *
                """,
                *params,
            )
        else:
            row = existing

        scope_type = data.get("scope_type", row["scope_type"])
        if schedules is not None:
            await _replace_schedules(conn, promotion_id, schedules)
        if category_ids is not None or product_ids is not None or "scope_type" in data:
            cats = category_ids if category_ids is not None else (
                await _load_scope_ids(conn, promotion_id)
            )[0]
            prods = product_ids if product_ids is not None else (
                await _load_scope_ids(conn, promotion_id)
            )[1]
            if scope_type == ScopeType.CATEGORIES.value and not cats:
                raise HTTPException(status_code=400, detail="category_ids required for categories scope")
            if scope_type == ScopeType.PRODUCTS.value and not prods:
                raise HTTPException(status_code=400, detail="product_ids required for products scope")
            await _replace_scope(
                conn, promotion_id, tenant_id, scope_type, cats, prods
            )

        schedules_rows = await _load_schedules(conn, promotion_id)
        cats, prods = await _load_scope_ids(conn, promotion_id)

    return {
        "success": True,
        "data": _serialize_promotion(row, schedules_rows, cats, prods),
    }


async def delete_promotion(request: Request, promotion_id: UUID) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        await _get_owned_promotion(conn, promotion_id, tenant_id)
        await conn.execute(
            "DELETE FROM tenant_promotions WHERE id = $1 AND tenant_id = $2",
            promotion_id,
            tenant_id,
        )

    return {"success": True, "message": "Promotion deleted successfully"}


async def list_active_promotions(
    request: Request,
    at: datetime,
    *,
    only_current: bool = False,
) -> dict:
    """List tenant promotions with is_currently_active for POS (read-only)."""
    result = await list_promotions(
        request,
        include_inactive=False,
        at=at,
    )
    if only_current:
        result["data"] = [
            row for row in result["data"] if row.get("is_currently_active")
        ]
        result["total"] = len(result["data"])
    return result


def default_at_bogota() -> datetime:
    return datetime.now(tz=BOGOTA)


def _promo_matches_line(
    promo: Dict[str, Any],
    *,
    product_id: UUID,
    category_id: Optional[UUID],
) -> bool:
    return product_in_scope(
        scope_type=promo["scope_type"],
        category_ids=promo["category_ids"],
        product_ids=promo["product_ids"],
        product_id=product_id,
        category_id=category_id,
    )


def _pick_best_promotion_for_line(
    promotions: Sequence[Dict[str, Any]],
    *,
    product_id: UUID,
    category_id: Optional[UUID],
) -> Optional[Dict[str, Any]]:
    matches = [
        p for p in promotions
        if _promo_matches_line(p, product_id=product_id, category_id=category_id)
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: (int(p.get("priority") or 0), p.get("name") or ""))


def _compute_line_promo_savings(line: Dict[str, Any], promo: Dict[str, Any]) -> int:
    """Return COP savings for one cart/order line (Toast-style BOGO uses full unit price)."""
    subtotal = float(line["subtotal"])
    quantity = int(line.get("quantity") or 1)
    if subtotal <= 0 or quantity <= 0:
        return 0

    promo_type = promo["promo_type"]
    value_json = promo.get("value_json") or {}

    if promo_type == "percent_off":
        pct = float(value_json.get("percent") or 0)
        if pct <= 0:
            return 0
        return min(round(subtotal * pct / 100), round(subtotal))

    if promo_type == "fixed_off":
        amount = float(value_json.get("amount_cop") or 0)
        if amount <= 0:
            return 0
        return min(round(amount), round(subtotal))

    if promo_type == "bogo":
        buy_qty = int(value_json.get("buy_qty") or 0)
        get_qty = int(value_json.get("get_qty") or 0)
        if buy_qty < 1 or get_qty < 1:
            return 0
        bundle = buy_qty + get_qty
        sets = quantity // bundle
        if sets <= 0:
            return 0
        unit_price = subtotal / quantity
        free_units = sets * get_qty
        return min(round(free_units * unit_price), round(subtotal))

    return 0


def evaluate_cart_promotions(
    lines: Sequence[Dict[str, Any]],
    promotions: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Evaluate active promotions against checkout lines.

    Each input line needs: id, product_id, category_id (optional), quantity, subtotal.
    Returns authoritative promo savings and per-line breakdown (batch #982).
    """
    evaluated_lines: List[Dict[str, Any]] = []
    breakdown_by_id: Dict[str, Dict[str, Any]] = {}
    total_promo_savings = 0
    original_subtotal = 0

    for line in lines:
        line_id = str(line["id"])
        product_id = line["product_id"]
        if isinstance(product_id, str):
            product_id = UUID(product_id)
        category_raw = line.get("category_id")
        category_id = UUID(category_raw) if category_raw else None
        subtotal = float(line["subtotal"])
        original_subtotal += subtotal

        promo = _pick_best_promotion_for_line(
            promotions,
            product_id=product_id,
            category_id=category_id,
        )
        promo_savings = 0
        promo_meta: Dict[str, Any] = {}
        if promo is not None:
            promo_savings = _compute_line_promo_savings(line, promo)
            if promo_savings > 0:
                promo_meta = {
                    "promotion_id": str(promo["id"]),
                    "promotion_name": promo["name"],
                    "promo_type": promo["promo_type"],
                }
                pid = promo_meta["promotion_id"]
                if pid not in breakdown_by_id:
                    breakdown_by_id[pid] = {
                        "promotion_id": pid,
                        "promotion_name": promo["name"],
                        "promo_type": promo["promo_type"],
                        "savings": 0,
                    }
                breakdown_by_id[pid]["savings"] += promo_savings

        total_promo_savings += promo_savings
        subtotal_after_promo = max(0.0, subtotal - promo_savings)
        evaluated_lines.append({
            "id": line_id,
            "product_id": str(product_id),
            "category_id": str(category_id) if category_id else None,
            "quantity": int(line.get("quantity") or 1),
            "subtotal": subtotal,
            "promo_savings": promo_savings,
            "subtotal_after_promo": subtotal_after_promo,
            **promo_meta,
        })

    subtotal_after_promos = max(0.0, original_subtotal - total_promo_savings)
    return {
        "lines": evaluated_lines,
        "subtotal": round(original_subtotal),
        "promo_savings": round(total_promo_savings),
        "subtotal_after_promos": round(subtotal_after_promos),
        "promo_breakdown": list(breakdown_by_id.values()),
    }


def apply_manual_discount_to_evaluated_lines(
    evaluated: Dict[str, Any],
    manual_discount_amount: float,
) -> Dict[str, Any]:
    """Apply manual order discount on promo-adjusted subtotals (stacking contract)."""
    manual_discount = max(0.0, float(manual_discount_amount or 0))
    lines = evaluated["lines"]
    base_total = float(evaluated["subtotal_after_promos"])
    if manual_discount <= 0 or base_total <= 0:
        for line in lines:
            line["manual_discount_allocated"] = 0
            line["total_discount_allocated"] = line["promo_savings"]
            line["net_total"] = line["subtotal"] - line["promo_savings"]
        return {
            **evaluated,
            "manual_discount_amount": 0,
            "total_amount": round(base_total),
        }

    manual_discount = min(round(manual_discount), round(base_total))
    dist_input = [
        {"subtotal": float(line["subtotal_after_promo"]), "_idx": idx}
        for idx, line in enumerate(lines)
    ]
    dist = _distribute_discount_from_promotions(dist_input, manual_discount)
    for idx, line in enumerate(lines):
        manual_alloc = dist[idx]["discount_allocated"]
        line["manual_discount_allocated"] = manual_alloc
        line["total_discount_allocated"] = line["promo_savings"] + manual_alloc
        line["net_total"] = line["subtotal"] - line["total_discount_allocated"]

    return {
        **evaluated,
        "manual_discount_amount": manual_discount,
        "total_amount": round(base_total - manual_discount),
    }


def _distribute_discount_from_promotions(items: List[dict], discount_amount: float) -> List[dict]:
    """Same proportional allocation as pos_cart_service._distribute_discount."""
    total_subtotal = sum(float(item["subtotal"]) for item in items)
    if total_subtotal <= 0 or discount_amount <= 0:
        for item in items:
            item["discount_allocated"] = 0.0
        return items

    allocated_total = 0.0
    for item in items:
        share = float(item["subtotal"]) / total_subtotal
        item["discount_allocated"] = round(discount_amount * share)
        allocated_total += item["discount_allocated"]

    remainder = round(discount_amount) - round(allocated_total)
    if remainder != 0:
        largest = max(items, key=lambda x: x["subtotal"])
        largest["discount_allocated"] += remainder
    return items


async def load_promotions_for_evaluation(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    at: datetime,
) -> List[Dict[str, Any]]:
    """Load tenant promotions that are active at `at`, with scope for POS evaluation."""
    rows = await conn.fetch(
        """
        SELECT * FROM tenant_promotions
        WHERE tenant_id = $1 AND is_active = true
        ORDER BY priority DESC, name
        """,
        tenant_id,
    )
    loaded: List[Dict[str, Any]] = []
    for row in rows:
        schedules = await _load_schedules(conn, row["id"])
        schedule_dicts = [_row_to_schedule(r) for r in schedules]
        if not is_active_at(
            at,
            is_active=row["is_active"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            schedules=schedule_dicts,
        ):
            continue
        category_ids, product_ids = await _load_scope_ids(conn, row["id"])
        loaded.append({
            "id": row["id"],
            "name": row["name"],
            "promo_type": row["promo_type"],
            "value_json": _parse_value_json(row["value_json"]),
            "scope_type": row["scope_type"],
            "priority": row["priority"],
            "stackable": row["stackable"],
            "category_ids": set(category_ids),
            "product_ids": set(product_ids),
        })
    return loaded


async def evaluate_checkout_promotions(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    lines: Sequence[Dict[str, Any]],
    *,
    at: Optional[datetime] = None,
    manual_discount_amount: float = 0,
    discount_type: Optional[str] = None,
    discount_value: Optional[float] = None,
) -> Dict[str, Any]:
    """DB-backed promo evaluation + optional manual discount stacking."""
    evaluation_at = at or default_at_bogota()
    promotions = await load_promotions_for_evaluation(conn, tenant_id, evaluation_at)
    evaluated = evaluate_cart_promotions(lines, promotions)
    manual_discount = float(manual_discount_amount or 0)
    if discount_type and discount_value is not None and discount_value > 0:
        if discount_type == "percent":
            manual_discount = round(evaluated["subtotal_after_promos"] * discount_value / 100)
        else:
            manual_discount = min(
                round(discount_value),
                round(evaluated["subtotal_after_promos"]),
            )
    return apply_manual_discount_to_evaluated_lines(evaluated, manual_discount)


def promo_persist_fields_from_eval_line(
    eval_line: Dict[str, Any],
) -> tuple[Optional[UUID], Optional[int]]:
    """Map evaluated checkout line → order_items promo columns (warocol.com#984)."""
    promo_id_raw = eval_line.get("promotion_id")
    savings = float(eval_line.get("promo_savings") or 0)
    promo_savings = round(savings) if savings > 0 else None
    promo_uuid: Optional[UUID] = None
    if promo_id_raw:
        promo_uuid = promo_id_raw if isinstance(promo_id_raw, UUID) else UUID(str(promo_id_raw))
    return promo_uuid, promo_savings
