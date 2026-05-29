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
