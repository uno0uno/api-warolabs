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

VALID_PROMO_TYPES = frozenset({"percent_off", "fixed_off", "bogo"})
DEFAULT_PROMO_CONFLICT_STRATEGY = "priority"
ALLOWED_PROMO_CONFLICT_STRATEGIES = frozenset({DEFAULT_PROMO_CONFLICT_STRATEGY})
DEFAULT_PROMO_TYPE_BLOCK_MAP: Dict[str, List[str]] = {
    "bogo": ["percent_off", "fixed_off"],
}

# Checkout contract (documented on promotion router + tenant context).
CONFLICT_RULES_DOC = (
    "Overlapping promotions may coexist in admin; at checkout exactly one automatic "
    "promotion applies per line. When multiple promotions match a line, the tenant "
    "promo_conflict_strategy applies (default: highest priority wins). "
    "Tenant promo_type_block_map defines type exclusions on the same line "
    "(default: BOGO blocks percent_off and fixed_off). "
    "stackable=false (default) means one promotion per line. "
    "Manual order-level discounts are applied separately in checkout."
)


def normalize_promo_type_block_map(
    raw: Optional[Any],
) -> Dict[str, List[str]]:
    """Return a validated type-block map, falling back to tenant defaults."""
    if raw is None:
        return dict(DEFAULT_PROMO_TYPE_BLOCK_MAP)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return dict(DEFAULT_PROMO_TYPE_BLOCK_MAP)
    if not isinstance(raw, dict) or not raw:
        return dict(DEFAULT_PROMO_TYPE_BLOCK_MAP)
    normalized: Dict[str, List[str]] = {}
    for winner, blocked in raw.items():
        if winner not in VALID_PROMO_TYPES:
            continue
        if not isinstance(blocked, list):
            continue
        cleaned = [b for b in blocked if isinstance(b, str) and b in VALID_PROMO_TYPES]
        if cleaned:
            normalized[winner] = cleaned
    return normalized or dict(DEFAULT_PROMO_TYPE_BLOCK_MAP)


def validate_promo_type_block_map(
    raw: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Validate a type-block map for PATCH writes; raises ValueError on bad input."""
    if not isinstance(raw, dict):
        raise ValueError("promo_type_block_map must be an object")
    normalized: Dict[str, List[str]] = {}
    for winner, blocked in raw.items():
        if winner not in VALID_PROMO_TYPES:
            raise ValueError(f"Unknown promo_type in block map: {winner}")
        if not isinstance(blocked, list):
            raise ValueError(f"Blocked types for {winner} must be a list")
        cleaned: List[str] = []
        for promo_type in blocked:
            if not isinstance(promo_type, str) or promo_type not in VALID_PROMO_TYPES:
                raise ValueError(f"Unknown blocked promo_type: {promo_type}")
            if promo_type not in cleaned:
                cleaned.append(promo_type)
        if cleaned:
            normalized[winner] = cleaned
    return normalized or dict(DEFAULT_PROMO_TYPE_BLOCK_MAP)


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


def _time_to_minutes(value: Any) -> int:
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    text = str(value)
    hours, minutes = text[:5].split(":")
    return int(hours) * 60 + int(minutes)


def schedules_overlap(
    a: Dict[str, Any],
    b: Dict[str, Any],
) -> bool:
    """Pairwise schedule overlap (parity with front promotionPreview.ts)."""
    if not (int(a["days_of_week"]) & int(b["days_of_week"])):
        return False
    a_start = _time_to_minutes(a["start_time"])
    a_end = _time_to_minutes(a["end_time"])
    b_start = _time_to_minutes(b["start_time"])
    b_end = _time_to_minutes(b["end_time"])
    if not a.get("crosses_midnight") and not b.get("crosses_midnight"):
        return a_start < b_end and b_start < a_end
    return True


def find_intra_promo_schedule_overlap(
    schedules: Sequence[Any],
) -> Optional[str]:
    """Return Spanish error when two rows in the same promo overlap."""
    rows: List[Dict[str, Any]] = []
    for sched in schedules:
        if isinstance(sched, dict):
            rows.append(sched)
        else:
            rows.append({
                "days_of_week": sched.days_of_week,
                "start_time": sched.start_time,
                "end_time": sched.end_time,
                "crosses_midnight": sched.crosses_midnight,
            })
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if schedules_overlap(rows[i], rows[j]):
                return (
                    f"Los horarios {i + 1} y {j + 1} "
                    "se superponen en los mismos días."
                )
    return None


def schedule_lists_overlap(
    schedules_a: Sequence[Dict[str, Any]],
    schedules_b: Sequence[Dict[str, Any]],
) -> bool:
    if not schedules_a or not schedules_b:
        return True
    return any(
        schedules_overlap(a, b)
        for a in schedules_a
        for b in schedules_b
    )


def campaign_dates_overlap(
    starts_a: Optional[datetime],
    ends_a: Optional[datetime],
    starts_b: Optional[datetime],
    ends_b: Optional[datetime],
) -> bool:
    if ends_a is not None and starts_b is not None and ends_a <= starts_b:
        return False
    if ends_b is not None and starts_a is not None and ends_b <= starts_a:
        return False
    return True


def product_scopes_overlap(
    product_ids_a: Optional[Set[UUID]],
    product_ids_b: Optional[Set[UUID]],
) -> bool:
    if product_ids_a is None or product_ids_b is None:
        return True
    return bool(product_ids_a & product_ids_b)


async def _expand_scope_product_ids(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    *,
    scope_type: str,
    category_ids: Sequence[UUID],
    product_ids: Sequence[UUID],
) -> Optional[Set[UUID]]:
    if scope_type == ScopeType.ALL_PRODUCTS.value:
        return None
    if scope_type == ScopeType.PRODUCTS.value:
        return set(product_ids)
    if not category_ids:
        return set()
    rows = await conn.fetch(
        """
        SELECT id FROM product
        WHERE tenant_id = $1 AND category_id = ANY($2::uuid[])
        """,
        tenant_id,
        list(category_ids),
    )
    return {row["id"] for row in rows}


async def _count_tenant_products(conn: asyncpg.Connection, tenant_id: UUID) -> int:
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS total FROM product WHERE tenant_id = $1",
        tenant_id,
    )
    return int(row["total"]) if row else 0


async def _shared_product_count(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    product_ids_a: Optional[Set[UUID]],
    product_ids_b: Optional[Set[UUID]],
) -> int:
    if product_ids_a is None and product_ids_b is None:
        return await _count_tenant_products(conn, tenant_id)
    if product_ids_a is None:
        return len(product_ids_b or set())
    if product_ids_b is None:
        return len(product_ids_a)
    return len(product_ids_a & product_ids_b)


def _normalize_schedule_input(sched: Any) -> Dict[str, Any]:
    """Accept PromotionScheduleInput or model_dump dict (PATCH body)."""
    if isinstance(sched, dict):
        return {
            "days_of_week": int(sched["days_of_week"]),
            "start_time": sched["start_time"],
            "end_time": sched["end_time"],
            "crosses_midnight": bool(sched.get("crosses_midnight", False)),
            "sort_order": int(sched.get("sort_order", 0)),
        }
    return {
        "days_of_week": int(sched.days_of_week),
        "start_time": sched.start_time,
        "end_time": sched.end_time,
        "crosses_midnight": bool(sched.crosses_midnight),
        "sort_order": int(getattr(sched, "sort_order", 0) or 0),
    }


def _schedule_dicts_from_inputs(schedules: Sequence[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for sched in schedules:
        normalized = _normalize_schedule_input(sched)
        rows.append({
            "days_of_week": normalized["days_of_week"],
            "start_time": normalized["start_time"],
            "end_time": normalized["end_time"],
            "crosses_midnight": normalized["crosses_midnight"],
        })
    return rows


async def detect_promotion_overlaps(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    candidate: Dict[str, Any],
    *,
    exclude_promotion_id: Optional[UUID] = None,
) -> List[Dict[str, Any]]:
    """Compare candidate promo against other active tenant promos."""
    peer_rows = await conn.fetch(
        """
        SELECT * FROM tenant_promotions
        WHERE tenant_id = $1 AND is_active = true
        ORDER BY priority DESC, name
        """,
        tenant_id,
    )
    candidate_products = await _expand_scope_product_ids(
        conn,
        tenant_id,
        scope_type=candidate["scope_type"],
        category_ids=candidate.get("category_ids") or [],
        product_ids=candidate.get("product_ids") or [],
    )
    candidate_schedules = candidate.get("schedules") or []

    warnings: List[Dict[str, Any]] = []
    for peer in peer_rows:
        if exclude_promotion_id and peer["id"] == exclude_promotion_id:
            continue
        peer_cats, peer_prods = await _load_scope_ids(conn, peer["id"])
        peer_products = await _expand_scope_product_ids(
            conn,
            tenant_id,
            scope_type=peer["scope_type"],
            category_ids=peer_cats,
            product_ids=peer_prods,
        )
        if not product_scopes_overlap(candidate_products, peer_products):
            continue
        if not campaign_dates_overlap(
            candidate.get("starts_at"),
            candidate.get("ends_at"),
            peer["starts_at"],
            peer["ends_at"],
        ):
            continue
        peer_schedules = [_row_to_schedule(r) for r in await _load_schedules(conn, peer["id"])]
        if not schedule_lists_overlap(candidate_schedules, peer_schedules):
            continue
        shared_count = await _shared_product_count(
            conn,
            tenant_id,
            candidate_products,
            peer_products,
        )
        peer_priority = int(peer["priority"])
        candidate_priority = int(candidate.get("priority") or 0)
        risk = "high" if peer_priority == candidate_priority else "medium"
        warnings.append({
            "promotion_id": str(peer["id"]),
            "promotion_name": peer["name"],
            "priority": peer_priority,
            "shared_product_count": shared_count,
            "risk": risk,
        })
    return warnings


def _requires_overlap_acknowledgment(
    warnings: Sequence[Dict[str, Any]],
    *,
    priority: int,
    overlap_acknowledged: bool,
) -> bool:
    if overlap_acknowledged or priority > 0:
        return False
    return any(w.get("risk") == "high" for w in warnings)


def _overlap_advisory_response(
    warnings: Sequence[Dict[str, Any]],
    *,
    requires_acknowledgment: bool,
    data: Optional[Dict[str, Any]] = None,
) -> dict:
    payload: Dict[str, Any] = {
        "success": True,
        "overlap_warnings": list(warnings),
        "requires_acknowledgment": requires_acknowledgment,
    }
    if data is not None:
        payload["data"] = data
    return payload


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


def _scope_summary_from_pairs(
    category_pairs: Sequence[tuple[UUID, Optional[str]]],
    product_pairs: Sequence[tuple[UUID, Optional[str]]],
) -> Dict[str, Any]:
    category_ids = [cid for cid, _ in category_pairs]
    product_ids = [pid for pid, _ in product_pairs]
    category_names = [name for _, name in category_pairs if name]
    product_names = [name for _, name in product_pairs if name]
    return {
        "category_ids": category_ids,
        "product_ids": product_ids,
        "category_count": len(category_ids),
        "product_count": len(product_ids),
        "category_names_preview": category_names[:3],
        "product_names_preview": product_names[:3],
    }


def _empty_scope_summary() -> Dict[str, Any]:
    return _scope_summary_from_pairs([], [])


def _group_scope_summaries(
    promotion_ids: Sequence[UUID],
    cat_rows: Sequence[asyncpg.Record],
    prod_rows: Sequence[asyncpg.Record],
) -> Dict[UUID, Dict[str, Any]]:
    cats_by_promo: Dict[UUID, List[tuple[UUID, Optional[str]]]] = {
        pid: [] for pid in promotion_ids
    }
    prods_by_promo: Dict[UUID, List[tuple[UUID, Optional[str]]]] = {
        pid: [] for pid in promotion_ids
    }
    for row in cat_rows:
        cats_by_promo[row["promotion_id"]].append((row["category_id"], row["name"]))
    for row in prod_rows:
        prods_by_promo[row["promotion_id"]].append((row["product_id"], row["name"]))
    return {
        pid: _scope_summary_from_pairs(cats_by_promo[pid], prods_by_promo[pid])
        for pid in promotion_ids
    }


def _serialize_promotion(
    promo_row: asyncpg.Record,
    schedules: List[asyncpg.Record],
    scope: Dict[str, Any],
    *,
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    schedule_dicts = [_row_to_schedule(r) for r in schedules]
    category_ids = scope["category_ids"]
    product_ids = scope["product_ids"]
    payload: Dict[str, Any] = {
        "id": str(promo_row["id"]),
        "tenant_id": str(promo_row["tenant_id"]),
        "name": promo_row["name"],
        "promo_type": promo_row["promo_type"],
        "value_json": _parse_value_json(promo_row["value_json"]),
        "scope_type": promo_row["scope_type"],
        "category_ids": [str(cid) for cid in category_ids],
        "product_ids": [str(pid) for pid in product_ids],
        "category_count": scope["category_count"],
        "product_count": scope["product_count"],
        "category_names_preview": list(scope["category_names_preview"]),
        "product_names_preview": list(scope["product_names_preview"]),
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
    scope = await _load_scope_summary(conn, promotion_id)
    return scope["category_ids"], scope["product_ids"]


async def _load_scope_summaries_batch(
    conn: asyncpg.Connection, promotion_ids: Sequence[UUID]
) -> Dict[UUID, Dict[str, Any]]:
    ids = list(promotion_ids)
    if not ids:
        return {}
    cat_rows = await conn.fetch(
        """
        SELECT sc.promotion_id, sc.category_id, c.name
        FROM tenant_promotion_scope_categories sc
        LEFT JOIN categories c ON c.id = sc.category_id
        WHERE sc.promotion_id = ANY($1::uuid[])
        ORDER BY sc.promotion_id, c.name NULLS LAST, sc.category_id
        """,
        ids,
    )
    prod_rows = await conn.fetch(
        """
        SELECT sp.promotion_id, sp.product_id, p.name
        FROM tenant_promotion_scope_products sp
        LEFT JOIN product p ON p.id = sp.product_id
        WHERE sp.promotion_id = ANY($1::uuid[])
        ORDER BY sp.promotion_id, p.name NULLS LAST, sp.product_id
        """,
        ids,
    )
    return _group_scope_summaries(ids, cat_rows, prod_rows)


async def _load_scope_summary(
    conn: asyncpg.Connection, promotion_id: UUID
) -> Dict[str, Any]:
    summaries = await _load_scope_summaries_batch(conn, [promotion_id])
    return summaries.get(promotion_id, _empty_scope_summary())


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
        row = _normalize_schedule_input(sched)
        await conn.execute(
            """
            INSERT INTO tenant_promotion_schedules (
                promotion_id, days_of_week, start_time, end_time,
                crosses_midnight, sort_order
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            promotion_id,
            row["days_of_week"],
            row["start_time"],
            row["end_time"],
            row["crosses_midnight"],
            row["sort_order"],
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
        scope_by_id = await _load_scope_summaries_batch(conn, [row["id"] for row in rows])
        data = []
        for row in rows:
            schedules = await _load_schedules(conn, row["id"])
            data.append(
                _serialize_promotion(
                    row,
                    schedules,
                    scope_by_id.get(row["id"], _empty_scope_summary()),
                    at=at,
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
        scope = await _load_scope_summary(conn, promotion_id)

    return {
        "success": True,
        "data": _serialize_promotion(row, schedules, scope, at=at),
    }


async def list_promotion_scope(
    request: Request,
    promotion_id: UUID,
    *,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Paginated scope item names for promotions list popover (warocol.com#999)."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    offset = (page - 1) * page_size
    search_term = search.strip() if search and search.strip() else None
    search_pattern = f"%{search_term}%" if search_term else None

    async with get_db_connection(use_transaction=False) as conn:
        row = await _get_owned_promotion(conn, promotion_id, tenant_id)
        scope_type = row["scope_type"]

        if scope_type == ScopeType.ALL_PRODUCTS.value:
            return {
                "success": True,
                "data": {
                    "scope_type": scope_type,
                    "promotion_name": row["name"],
                    "items": [],
                    "total": 0,
                    "page": page,
                    "page_size": page_size,
                },
            }

        if scope_type == ScopeType.CATEGORIES.value:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM tenant_promotion_scope_categories sc
                LEFT JOIN categories c ON c.id = sc.category_id
                WHERE sc.promotion_id = $1
                  AND ($2::text IS NULL OR c.name ILIKE $2)
                """,
                promotion_id,
                search_pattern,
            )
            rows = await conn.fetch(
                """
                SELECT sc.category_id AS id, c.name
                FROM tenant_promotion_scope_categories sc
                LEFT JOIN categories c ON c.id = sc.category_id
                WHERE sc.promotion_id = $1
                  AND ($2::text IS NULL OR c.name ILIKE $2)
                ORDER BY c.name NULLS LAST, sc.category_id
                LIMIT $3 OFFSET $4
                """,
                promotion_id,
                search_pattern,
                page_size,
                offset,
            )
        else:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM tenant_promotion_scope_products sp
                LEFT JOIN product p ON p.id = sp.product_id
                WHERE sp.promotion_id = $1
                  AND ($2::text IS NULL OR p.name ILIKE $2)
                """,
                promotion_id,
                search_pattern,
            )
            rows = await conn.fetch(
                """
                SELECT sp.product_id AS id, p.name
                FROM tenant_promotion_scope_products sp
                LEFT JOIN product p ON p.id = sp.product_id
                WHERE sp.promotion_id = $1
                  AND ($2::text IS NULL OR p.name ILIKE $2)
                ORDER BY p.name NULLS LAST, sp.product_id
                LIMIT $3 OFFSET $4
                """,
                promotion_id,
                search_pattern,
                page_size,
                offset,
            )

        total = int(count_row["total"]) if count_row else 0

    items = [{"id": str(r["id"]), "name": r["name"] or "(sin nombre)"} for r in rows]
    return {
        "success": True,
        "data": {
            "scope_type": scope_type,
            "promotion_name": row["name"],
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


async def create_promotion(request: Request, body: PromotionCreate) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    intra_overlap = find_intra_promo_schedule_overlap(body.schedules)
    if intra_overlap:
        raise HTTPException(status_code=400, detail=intra_overlap)

    candidate = {
        "scope_type": body.scope_type.value,
        "category_ids": body.category_ids,
        "product_ids": body.product_ids,
        "schedules": _schedule_dicts_from_inputs(body.schedules),
        "starts_at": body.starts_at,
        "ends_at": body.ends_at,
        "priority": body.priority,
    }

    try:
        async with get_db_connection() as conn:
            overlap_warnings = await detect_promotion_overlaps(
                conn,
                tenant_id,
                candidate,
            )
            if _requires_overlap_acknowledgment(
                overlap_warnings,
                priority=body.priority,
                overlap_acknowledged=body.overlap_acknowledged,
            ):
                return _overlap_advisory_response(
                    overlap_warnings,
                    requires_acknowledgment=True,
                )

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
            scope = await _load_scope_summary(conn, promotion_id)
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una promoción con ese nombre",
        ) from None

    logger.info("Created promotion %r for tenant %s", body.name, tenant_id)
    return _overlap_advisory_response(
        overlap_warnings,
        requires_acknowledgment=False,
        data=_serialize_promotion(row, schedules, scope),
    )


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
    overlap_acknowledged = data.pop("overlap_acknowledged", False)

    if schedules is not None:
        intra_overlap = find_intra_promo_schedule_overlap(schedules)
        if intra_overlap:
            raise HTTPException(status_code=400, detail=intra_overlap)

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

        scope_type = data.get("scope_type", existing["scope_type"])
        merged_cats = category_ids if category_ids is not None else (
            await _load_scope_ids(conn, promotion_id)
        )[0]
        merged_prods = product_ids if product_ids is not None else (
            await _load_scope_ids(conn, promotion_id)
        )[1]
        merged_schedules_rows = (
            await _load_schedules(conn, promotion_id)
            if schedules is None
            else schedules
        )
        merged_priority = data.get("priority", existing["priority"])
        merged_starts = data.get("starts_at", existing["starts_at"])
        merged_ends = data.get("ends_at", existing["ends_at"])

        candidate = {
            "scope_type": scope_type,
            "category_ids": merged_cats,
            "product_ids": merged_prods,
            "schedules": _schedule_dicts_from_inputs(merged_schedules_rows),
            "starts_at": merged_starts,
            "ends_at": merged_ends,
            "priority": merged_priority,
        }
        overlap_warnings = await detect_promotion_overlaps(
            conn,
            tenant_id,
            candidate,
            exclude_promotion_id=promotion_id,
        )
        if _requires_overlap_acknowledgment(
            overlap_warnings,
            priority=int(merged_priority),
            overlap_acknowledged=overlap_acknowledged,
        ):
            return _overlap_advisory_response(
                overlap_warnings,
                requires_acknowledgment=True,
            )

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
        scope = await _load_scope_summary(conn, promotion_id)

    return _overlap_advisory_response(
        overlap_warnings,
        requires_acknowledgment=False,
        data=_serialize_promotion(row, schedules_rows, scope),
    )


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


def _scope_specificity_rank(scope_type: str) -> int:
    if scope_type == ScopeType.PRODUCTS.value:
        return 2
    if scope_type == ScopeType.CATEGORIES.value:
        return 1
    return 0


def _promo_rank_key(promo: Dict[str, Any]) -> tuple:
    return (
        int(promo.get("priority") or 0),
        _scope_specificity_rank(str(promo.get("scope_type") or "")),
        promo.get("name") or "",
    )


def _promo_block_rank_key(promo: Dict[str, Any]) -> tuple:
    return (
        int(promo.get("priority") or 0),
        _scope_specificity_rank(str(promo.get("scope_type") or "")),
    )


def _filter_type_blocked_candidates(
    matches: Sequence[Dict[str, Any]],
    type_block_map: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    if not matches:
        return []
    eligible: List[Dict[str, Any]] = []
    for candidate in matches:
        candidate_type = str(candidate.get("promo_type") or "")
        candidate_rank = _promo_block_rank_key(candidate)
        blocked = False
        for other in matches:
            if other is candidate:
                continue
            blocked_types = type_block_map.get(str(other.get("promo_type") or "")) or []
            if candidate_type not in blocked_types:
                continue
            other_rank = _promo_block_rank_key(other)
            if other_rank > candidate_rank:
                blocked = True
                break
            if other_rank == candidate_rank:
                blocked = True
                break
        if not blocked:
            eligible.append(candidate)
    return eligible


def _pick_best_promotion_for_line(
    promotions: Sequence[Dict[str, Any]],
    *,
    product_id: UUID,
    category_id: Optional[UUID],
    promo_type_block_map: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    matches = [
        p for p in promotions
        if _promo_matches_line(p, product_id=product_id, category_id=category_id)
    ]
    if not matches:
        return None
    block_map = normalize_promo_type_block_map(promo_type_block_map)
    eligible = _filter_type_blocked_candidates(matches, block_map)
    if not eligible:
        return None
    return max(eligible, key=_promo_rank_key)


def _promo_eligible_subtotal(line: Dict[str, Any]) -> float:
    subtotal = float(line["subtotal"])
    raw = line.get("promo_eligible_subtotal")
    if raw is None:
        return subtotal
    return min(subtotal, max(0.0, float(raw)))


def _promo_eligible_unit_price(line: Dict[str, Any]) -> float:
    quantity = int(line.get("quantity") or 1)
    if quantity <= 0:
        return 0.0
    raw = line.get("promo_eligible_unit_price")
    if raw is not None:
        return min(max(0.0, float(raw)), _promo_eligible_subtotal(line) / quantity)
    return _promo_eligible_subtotal(line) / quantity


def _compute_line_promo_savings(line: Dict[str, Any], promo: Dict[str, Any]) -> int:
    """Return COP savings for one cart/order line using its promo-eligible basis."""
    subtotal = float(line["subtotal"])
    eligible_subtotal = _promo_eligible_subtotal(line)
    quantity = int(line.get("quantity") or 1)
    if subtotal <= 0 or eligible_subtotal <= 0 or quantity <= 0:
        return 0

    promo_type = promo["promo_type"]
    value_json = promo.get("value_json") or {}

    if promo_type == "percent_off":
        pct = float(value_json.get("percent") or 0)
        if pct <= 0:
            return 0
        return min(round(eligible_subtotal * pct / 100), round(eligible_subtotal))

    if promo_type == "fixed_off":
        amount = float(value_json.get("amount_cop") or 0)
        if amount <= 0:
            return 0
        return min(round(amount), round(eligible_subtotal))

    if promo_type == "bogo":
        buy_qty = int(value_json.get("buy_qty") or 0)
        get_qty = int(value_json.get("get_qty") or 0)
        if buy_qty < 1 or get_qty < 1:
            return 0
        bundle = buy_qty + get_qty
        sets = quantity // bundle
        if sets <= 0:
            return 0
        unit_price = _promo_eligible_unit_price(line)
        free_units = sets * get_qty
        return min(round(free_units * unit_price), round(eligible_subtotal))

    return 0


def _line_promo_eligible_unit_price(line: Dict[str, Any]) -> float:
    quantity = int(line.get("quantity") or 1)
    if quantity <= 0:
        return 0.0
    return _promo_eligible_subtotal(line) / quantity


def _allocate_bogo_savings_cheapest_first(
    entries: Sequence[Dict[str, Any]],
    promo: Dict[str, Any],
) -> Dict[str, int]:
    """
    Cross-line BOGO (warocol.com#1023): expand sibling lines to units, form
    buy_qty+get_qty bundles, mark free units cheapest-first across lines.
    """
    value_json = promo.get("value_json") or {}
    buy_qty = int(value_json.get("buy_qty") or 0)
    get_qty = int(value_json.get("get_qty") or 0)
    if buy_qty < 1 or get_qty < 1:
        return {str(entry["line_id"]): 0 for entry in entries}

    bundle = buy_qty + get_qty
    units: List[tuple[str, float]] = []
    cap_by_line: Dict[str, float] = {}
    for entry in entries:
        line_id = str(entry["line_id"])
        qty = max(0, int(entry.get("quantity") or 1))
        unit_price = float(entry["unit_price"])
        cap_by_line[line_id] = float(
            entry.get("promo_eligible_subtotal")
            if entry.get("promo_eligible_subtotal") is not None
            else entry.get("subtotal") or (unit_price * qty)
        )
        for _ in range(qty):
            units.append((line_id, unit_price))

    sets = len(units) // bundle
    free_count = sets * get_qty
    savings_by_line: Dict[str, float] = {lid: 0.0 for lid in cap_by_line}
    if free_count <= 0:
        return {lid: 0 for lid in cap_by_line}

    units.sort(key=lambda item: item[1])
    for line_id, unit_price in units[:free_count]:
        savings_by_line[line_id] += unit_price

    return {
        line_id: min(round(savings), round(cap_by_line[line_id]))
        for line_id, savings in savings_by_line.items()
    }


def evaluate_cart_promotions(
    lines: Sequence[Dict[str, Any]],
    promotions: Sequence[Dict[str, Any]],
    *,
    promo_type_block_map: Optional[Dict[str, Any]] = None,
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

    pending: List[Dict[str, Any]] = []
    bogo_group_indices: Dict[tuple[str, str], List[int]] = {}

    for line in lines:
        line_id = str(line["id"])
        product_id = line["product_id"]
        if isinstance(product_id, str):
            product_id = UUID(product_id)
        category_raw = line.get("category_id")
        category_id = UUID(category_raw) if category_raw else None
        subtotal = float(line["subtotal"])
        original_subtotal += subtotal

        if line.get("promo_opt_out"):
            promo = None
        else:
            promo = _pick_best_promotion_for_line(
                promotions,
                product_id=product_id,
                category_id=category_id,
                promo_type_block_map=promo_type_block_map,
            )

        idx = len(pending)
        pending.append({
            "line": line,
            "line_id": line_id,
            "product_id": product_id,
            "category_id": category_id,
            "subtotal": subtotal,
            "promo": promo,
        })

        if promo is not None and promo["promo_type"] == "bogo":
            group_key = (str(product_id), str(promo["id"]))
            bogo_group_indices.setdefault(group_key, []).append(idx)

    savings_by_line_id: Dict[str, int] = {}

    for indices in bogo_group_indices.values():
        group_states = [pending[i] for i in indices]
        promo = group_states[0]["promo"]
        entries = [
            {
                "line_id": state["line_id"],
                "quantity": int(state["line"].get("quantity") or 1),
                "unit_price": _line_promo_eligible_unit_price(state["line"]),
                "subtotal": state["subtotal"],
                "promo_eligible_subtotal": _promo_eligible_subtotal(state["line"]),
            }
            for state in group_states
        ]
        allocated = _allocate_bogo_savings_cheapest_first(entries, promo)
        savings_by_line_id.update(allocated)

    for state in pending:
        line_id = state["line_id"]
        promo = state["promo"]
        if promo is None:
            savings_by_line_id.setdefault(line_id, 0)
            continue
        if promo["promo_type"] == "bogo":
            savings_by_line_id.setdefault(line_id, 0)
            continue
        savings_by_line_id[line_id] = _compute_line_promo_savings(state["line"], promo)

    for state in pending:
        line_id = state["line_id"]
        subtotal = state["subtotal"]
        promo = state["promo"]
        promo_savings = savings_by_line_id.get(line_id, 0)
        promo_meta: Dict[str, Any] = {}
        if promo is not None and promo_savings > 0:
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
            "product_id": str(state["product_id"]),
            "category_id": str(state["category_id"]) if state["category_id"] else None,
            "quantity": int(state["line"].get("quantity") or 1),
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


def apply_waro_redemption_to_evaluated_lines(
    evaluated: Dict[str, Any],
    waro_discount_cop: float,
) -> Dict[str, Any]:
    """Apply WaRo COP discount on promo+manual-adjusted subtotals (checkout layer 4)."""
    waro_discount = max(0.0, float(waro_discount_cop or 0))
    lines = evaluated["lines"]
    base_total = float(evaluated["total_amount"])
    if waro_discount <= 0 or base_total <= 0:
        for line in lines:
            line["waro_discount_allocated"] = 0
        return {
            **evaluated,
            "waro_redemption_amount_cop": 0,
            "total_amount": round(base_total),
        }

    waro_discount = min(round(waro_discount), round(base_total))
    dist_input = [
        {"subtotal": float(line["net_total"]), "_idx": idx}
        for idx, line in enumerate(lines)
    ]
    dist = _distribute_discount_from_promotions(dist_input, waro_discount)
    for idx, line in enumerate(lines):
        waro_alloc = dist[idx]["discount_allocated"]
        line["waro_discount_allocated"] = waro_alloc
        line["total_discount_allocated"] = (
            float(line.get("total_discount_allocated") or line.get("promo_savings") or 0)
            + waro_alloc
        )
        line["net_total"] = float(line["subtotal"]) - line["total_discount_allocated"]

    return {
        **evaluated,
        "waro_redemption_amount_cop": waro_discount,
        "total_amount": round(base_total - waro_discount),
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
    profile_row = await conn.fetchrow(
        """
        SELECT promo_type_block_map
        FROM tenant_public_profiles
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    type_block_map = normalize_promo_type_block_map(
        profile_row["promo_type_block_map"] if profile_row else None
    )
    evaluated = evaluate_cart_promotions(
        lines,
        promotions,
        promo_type_block_map=type_block_map,
    )
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


_SESSION_PENDING_PROMO_ITEMS_SQL = """
    SELECT oi.id, oi.subtotal, oi.quantity, oi.price_at_purchase,
           oi.product_id, oi.promo_opt_out,
           p.category_id,
           COALESCE(p.tax_category, 'standard') AS tax_category
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    JOIN product p ON p.id = oi.product_id
    WHERE o.table_session_id = $1 AND o.status = 'pending'
"""


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if hasattr(row, "get"):
            return row.get(key, default)
        return default


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def item_rows_to_promo_lines(item_rows: Sequence[Any]) -> List[Dict[str, Any]]:
    """Map pending order_item rows → evaluate_checkout_promotions input."""
    lines: List[Dict[str, Any]] = []
    for row in item_rows:
        quantity = int(row["quantity"])
        line = {
            "id": str(row["id"]),
            "product_id": str(row["product_id"]),
            "category_id": str(row["category_id"]) if row["category_id"] else None,
            "quantity": quantity,
            "subtotal": float(row["subtotal"]),
            "tax_category": row["tax_category"] or "standard",
            "promo_opt_out": bool(_row_value(row, "promo_opt_out")),
        }
        explicit_eligible = _row_value(row, "promo_eligible_subtotal")
        base_price = _row_value(row, "price_at_purchase")
        eligible_modifier_total = _row_value(row, "promo_eligible_modifier_unit_total", 0)
        if explicit_eligible is not None:
            line["promo_eligible_subtotal"] = float(explicit_eligible)
        elif base_price is not None:
            line["promo_eligible_subtotal"] = (
                float(base_price) + float(eligible_modifier_total or 0)
            ) * quantity
        lines.append(line)
    return lines


async def enrich_order_item_rows_with_promo_basis(
    conn: asyncpg.Connection,
    item_rows: Sequence[Any],
) -> List[Dict[str, Any]]:
    """Attach required/default modifier totals used as the promo-eligible basis."""
    rows = [_row_to_dict(row) for row in item_rows]
    order_item_ids = [row["id"] for row in rows]
    if not order_item_ids:
        return rows

    basis_rows = await conn.fetch(
        """
        SELECT
            oim.order_item_id,
            SUM(
                CASE
                    WHEN COALESCE(mg.is_required, false) OR COALESCE(m.is_default, false)
                    THEN oim.price_at_purchase * COALESCE(oim.quantity, 1)
                    ELSE 0
                END
            ) AS eligible_modifier_unit_total
        FROM order_item_modifiers oim
        JOIN modifiers m ON m.id = oim.modifier_id
        JOIN modifier_groups mg ON mg.id = m.modifier_group_id
        WHERE oim.order_item_id = ANY($1::uuid[])
        GROUP BY oim.order_item_id
        """,
        order_item_ids,
    )
    basis_by_id = {
        row["order_item_id"]: float(row["eligible_modifier_unit_total"] or 0)
        for row in basis_rows
    }
    for row in rows:
        row["promo_eligible_modifier_unit_total"] = basis_by_id.get(row["id"], 0.0)
    return rows


async def apply_promo_eval_to_order_items(
    conn: asyncpg.Connection,
    item_rows: Sequence[Any],
    checkout_eval: Dict[str, Any],
) -> None:
    """Persist evaluate_checkout_promotions line results on order_items (warocol.com#1020)."""
    eval_by_id = {line["id"]: line for line in checkout_eval["lines"]}
    for row in item_rows:
        eval_line = eval_by_id.get(str(row["id"]), {})
        total_alloc = eval_line.get("total_discount_allocated")
        net_total = eval_line.get("net_total")
        promo_id, promo_savings = promo_persist_fields_from_eval_line(eval_line)
        if total_alloc or net_total is not None or promo_id or promo_savings:
            await conn.execute(
                """
                UPDATE order_items
                SET discount_allocated = $2, net_total = $3,
                    applied_promotion_id = $4, promo_savings_allocated = $5
                WHERE id = $1::uuid
                """,
                row["id"],
                total_alloc,
                net_total,
                promo_id,
                promo_savings,
            )


async def recalc_pending_session_order_totals(
    conn: asyncpg.Connection,
    session_id: UUID,
) -> None:
    """Set pending orders.total_amount from net line totals after promo persist."""
    await conn.execute(
        """
        UPDATE orders o
        SET total_amount = sub.sum_net
        FROM (
            SELECT oi.order_id, COALESCE(SUM(COALESCE(oi.net_total, oi.subtotal)), 0) AS sum_net
            FROM order_items oi
            JOIN orders ord ON ord.id = oi.order_id
            WHERE ord.table_session_id = $1 AND ord.status = 'pending'
            GROUP BY oi.order_id
        ) sub
        WHERE o.id = sub.order_id
          AND o.table_session_id = $1
          AND o.status = 'pending'
        """,
        session_id,
    )


async def persist_session_tab_promos(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    session_id: UUID,
) -> Dict[str, Any]:
    """Evaluate and persist promo fields for all pending tab lines in a session."""
    item_rows = await conn.fetch(_SESSION_PENDING_PROMO_ITEMS_SQL, session_id)
    if not item_rows:
        return {
            "lines": [],
            "promo_savings": 0,
            "subtotal_after_promos": 0,
            "promo_breakdown": [],
        }

    enriched_rows = await enrich_order_item_rows_with_promo_basis(conn, item_rows)
    checkout_eval = await evaluate_checkout_promotions(
        conn,
        tenant_id,
        item_rows_to_promo_lines(enriched_rows),
    )
    await apply_promo_eval_to_order_items(conn, item_rows, checkout_eval)
    await recalc_pending_session_order_totals(conn, session_id)
    return checkout_eval
