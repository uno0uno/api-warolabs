"""Menu category helpers — online menu ordering (api-warolabs#690)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request

from app.core.exceptions import APIError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.category import Category

logger = logging.getLogger(__name__)

ONLINE_MENU_CATEGORY_ORDER_BY = "o.display_order NULLS LAST, c.name ASC"

_ONLINE_MENU_PRODUCT_JOIN = """
    JOIN product p ON p.category_id = c.id
        AND p.tenant_id = $1
        AND p.is_available = true
        AND p.is_available_online = true
"""

_ONLINE_MENU_ORDER_JOIN = """
    LEFT JOIN tenant_online_menu_category_orders o
        ON o.category_id = c.id
        AND o.tenant_id = $1
"""


def online_menu_categories_select_sql() -> str:
    """SELECT fragment for public online menu categories (id, name, description)."""
    return f"""
        SELECT sub.id, sub.name, sub.description
        FROM (
            SELECT DISTINCT ON (c.id)
                c.id, c.name, c.description, o.display_order
            FROM categories c
            {_ONLINE_MENU_PRODUCT_JOIN}
            {_ONLINE_MENU_ORDER_JOIN}
            ORDER BY c.id, o.display_order NULLS LAST
        ) sub
        ORDER BY sub.display_order NULLS LAST, sub.name ASC
    """


def online_menu_products_order_by_sql() -> str:
    """ORDER BY suffix for products grouped under ordered categories."""
    return "o.display_order NULLS LAST, c.name ASC, p.name ASC"


async def fetch_eligible_online_menu_category_ids(conn, tenant_id: UUID) -> List[UUID]:
    rows = await conn.fetch(
        f"""
        SELECT c.id
        FROM categories c
        {_ONLINE_MENU_PRODUCT_JOIN}
        WHERE (c.tenant_id IS NULL OR c.tenant_id = $1)
        GROUP BY c.id
        ORDER BY MIN(c.name) ASC
        """,
        tenant_id,
    )
    return [row["id"] for row in rows]


async def list_online_menu_categories(request: Request) -> Dict[str, Any]:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise APIError("Tenant context is required", status_code=400)

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                sub.id, sub.name, sub.description, sub.tenant_id,
                sub.created_at, sub.updated_at
            FROM (
                SELECT DISTINCT ON (c.id)
                    c.id, c.name, c.description, c.tenant_id,
                    c.created_at, c.updated_at, o.display_order
                FROM categories c
                {_ONLINE_MENU_PRODUCT_JOIN}
                {_ONLINE_MENU_ORDER_JOIN}
                WHERE (c.tenant_id IS NULL OR c.tenant_id = $1)
                ORDER BY c.id, o.display_order NULLS LAST
            ) sub
            ORDER BY sub.display_order NULLS LAST, sub.name ASC
            """,
            tenant_id,
        )

    categories = [Category(**dict(row)) for row in rows]
    return {
        "success": True,
        "total": len(categories),
        "data": categories,
    }


async def reorder_online_menu_categories(request: Request, category_ids: List[UUID]) -> Dict[str, Any]:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise APIError("Tenant context is required", status_code=400)

    if not category_ids:
        raise APIError("category_ids is required", status_code=400)

    unique_ids = list(dict.fromkeys(category_ids))
    if len(unique_ids) != len(category_ids):
        raise APIError("category_ids contains duplicates", status_code=400)

    async with get_db_connection() as conn:
        async with conn.transaction():
            eligible_ids = await fetch_eligible_online_menu_category_ids(conn, tenant_id)
            eligible_set = set(eligible_ids)

            if set(unique_ids) != eligible_set:
                raise APIError(
                    "category_ids must include every online-menu category exactly once",
                    status_code=400,
                )

            visible_rows = await conn.fetch(
                """
                SELECT id
                FROM categories
                WHERE id = ANY($1::uuid[])
                  AND (tenant_id IS NULL OR tenant_id = $2)
                """,
                unique_ids,
                tenant_id,
            )
            if len(visible_rows) != len(unique_ids):
                raise APIError("One or more categories were not found", status_code=404)

            await conn.execute(
                """
                DELETE FROM tenant_online_menu_category_orders
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            await conn.execute(
                """
                INSERT INTO tenant_online_menu_category_orders (tenant_id, category_id, display_order)
                SELECT $1, id, ord::integer
                FROM UNNEST($2::uuid[]) WITH ORDINALITY AS u(id, ord)
                """,
                tenant_id,
                unique_ids,
            )

    return {
        "success": True,
        "message": "Orden de categorías del menú en línea actualizado",
        "data": {
            "category_ids": [str(category_id) for category_id in unique_ids],
        },
    }
