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
from app.services.operation_events_service import DOMAIN_MENU, record_module_event

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

_ONLINE_MENU_PRODUCT_ORDER_JOIN = """
    LEFT JOIN tenant_online_menu_product_orders po
        ON po.product_id = p.id
        AND po.tenant_id = $1
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


def online_menu_product_order_join_sql() -> str:
    """LEFT JOIN fragment for per-product online menu order (alias po)."""
    return _ONLINE_MENU_PRODUCT_ORDER_JOIN


def online_menu_products_order_by_sql() -> str:
    """ORDER BY suffix for products grouped under ordered categories."""
    return (
        "o.display_order NULLS LAST, c.name ASC, "
        "po.display_order NULLS LAST, p.name ASC"
    )


def table_qr_categories_select_sql() -> str:
    """Categories for mesa QR using the same tenant category display_order."""
    return f"""
        SELECT sub.id, sub.name, sub.description
        FROM (
            SELECT DISTINCT ON (c.id)
                c.id, c.name, c.description, o.display_order
            FROM categories c
            JOIN product p ON p.category_id = c.id
                AND p.tenant_id = $1
                AND p.is_available = true
                AND p.is_available_table_qr = true
            {_ONLINE_MENU_ORDER_JOIN}
            ORDER BY c.id, o.display_order NULLS LAST
        ) sub
        ORDER BY sub.display_order NULLS LAST, sub.name ASC
    """


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


async def fetch_eligible_online_menu_product_ids(
    conn,
    tenant_id: UUID,
    category_id: UUID,
) -> List[UUID]:
    """Products in a category eligible for Negocio online-menu ordering (online or QR)."""
    rows = await conn.fetch(
        """
        SELECT p.id
        FROM product p
        WHERE p.tenant_id = $1
          AND p.category_id = $2
          AND p.is_available = true
          AND (p.is_available_online = true OR p.is_available_table_qr = true)
        ORDER BY p.name ASC
        """,
        tenant_id,
        category_id,
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
                sub.id, sub.name, sub.description, sub.color, sub.tenant_id,
                sub.created_at, sub.updated_at
            FROM (
                SELECT DISTINCT ON (c.id)
                    c.id, c.name, c.description, c.color, c.tenant_id,
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


async def list_online_menu_products(request: Request, category_id: UUID) -> Dict[str, Any]:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise APIError("Tenant context is required", status_code=400)

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                p.id, p.name, p.category_id,
                p.is_available_online, p.is_available_table_qr,
                po.display_order
            FROM product p
            {_ONLINE_MENU_PRODUCT_ORDER_JOIN}
            WHERE p.tenant_id = $1
              AND p.category_id = $2
              AND p.is_available = true
              AND (p.is_available_online = true OR p.is_available_table_qr = true)
            ORDER BY po.display_order NULLS LAST, p.name ASC
            """,
            tenant_id,
            category_id,
        )

    data = [
        {
            "id": row["id"],
            "name": row["name"],
            "category_id": row["category_id"],
            "is_available_online": bool(row["is_available_online"]),
            "is_available_table_qr": bool(row["is_available_table_qr"]),
        }
        for row in rows
    ]
    return {
        "success": True,
        "total": len(data),
        "data": data,
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

            await record_module_event(
                conn,
                tenant_id,
                domain=DOMAIN_MENU,
                action="menu_reordered",
                actor_user_id=getattr(session_context, "user_id", None),
                entity_type="online_menu",
                entity_id=tenant_id,
                label="categories",
                extra={"count": len(unique_ids)},
            )

    return {
        "success": True,
        "message": "Orden de categorías del menú en línea actualizado",
        "data": {
            "category_ids": [str(category_id) for category_id in unique_ids],
        },
    }


async def reorder_online_menu_products(
    request: Request,
    category_id: UUID,
    product_ids: List[UUID],
) -> Dict[str, Any]:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise APIError("Tenant context is required", status_code=400)

    if not product_ids:
        raise APIError("product_ids is required", status_code=400)

    unique_ids = list(dict.fromkeys(product_ids))
    if len(unique_ids) != len(product_ids):
        raise APIError("product_ids contains duplicates", status_code=400)

    async with get_db_connection() as conn:
        async with conn.transaction():
            eligible_ids = await fetch_eligible_online_menu_product_ids(
                conn, tenant_id, category_id,
            )
            eligible_set = set(eligible_ids)

            if not eligible_set:
                raise APIError(
                    "No eligible online-menu products in this category",
                    status_code=400,
                )

            if set(unique_ids) != eligible_set:
                raise APIError(
                    "product_ids must include every eligible product in the category exactly once",
                    status_code=400,
                )

            owned_rows = await conn.fetch(
                """
                SELECT id
                FROM product
                WHERE id = ANY($1::uuid[])
                  AND tenant_id = $2
                  AND category_id = $3
                """,
                unique_ids,
                tenant_id,
                category_id,
            )
            if len(owned_rows) != len(unique_ids):
                raise APIError("One or more products were not found in this category", status_code=404)

            # Replace order rows for this category's products only (keep other categories).
            await conn.execute(
                """
                DELETE FROM tenant_online_menu_product_orders
                WHERE tenant_id = $1
                  AND product_id = ANY($2::uuid[])
                """,
                tenant_id,
                unique_ids,
            )
            await conn.execute(
                """
                INSERT INTO tenant_online_menu_product_orders (tenant_id, product_id, display_order)
                SELECT $1, id, ord::integer
                FROM UNNEST($2::uuid[]) WITH ORDINALITY AS u(id, ord)
                """,
                tenant_id,
                unique_ids,
            )

            await record_module_event(
                conn,
                tenant_id,
                domain=DOMAIN_MENU,
                action="menu_reordered",
                actor_user_id=getattr(session_context, "user_id", None),
                entity_type="online_menu",
                entity_id=category_id,
                label="products",
                extra={"count": len(unique_ids)},
            )

    return {
        "success": True,
        "message": "Orden de productos del menú en línea actualizado",
        "data": {
            "category_id": str(category_id),
            "product_ids": [str(product_id) for product_id in unique_ids],
        },
    }
