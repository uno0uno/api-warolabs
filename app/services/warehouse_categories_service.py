import re
import unicodedata
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException
from app.services.operation_events_service import DOMAIN_ABASTECIMIENTO, record_module_event


def clean_warehouse_category_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def normalize_warehouse_category_name(value: str) -> str:
    cleaned = clean_warehouse_category_name(value)
    decomposed = unicodedata.normalize("NFD", cleaned)
    without_diacritics = "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    )
    return without_diacritics.lower()


def _category_dict(row, tenant_id: UUID) -> Dict[str, Any]:
    data = dict(row)
    owner_id = data.get("tenant_id")
    data["scope"] = "global" if owner_id is None else "tenant"
    data["can_manage"] = owner_id == tenant_id
    for field in ("ingredient_count", "global_count", "tenant_count"):
        data[field] = int(data.get(field) or 0)
    return data


async def list_warehouse_categories(
    conn,
    tenant_id: UUID,
    search: Optional[str] = None,
    limit: int = 100,
    include_archived: bool = False,
) -> List[Dict[str, Any]]:
    params: List[Any] = [tenant_id]
    query = """
        SELECT
            wc.id,
            wc.tenant_id,
            wc.name,
            wc.normalized_name,
            wc.is_active,
            wc.created_at,
            wc.updated_at,
            COUNT(i.id)::int AS ingredient_count,
            COUNT(i.id) FILTER (WHERE i.tenant_id IS NULL)::int AS global_count,
            COUNT(i.id) FILTER (WHERE i.tenant_id = $1)::int AS tenant_count
        FROM warehouse_categories wc
        LEFT JOIN ingredients i
          ON i.warehouse_category_id = wc.id
         AND i.is_active = TRUE
         AND (i.tenant_id IS NULL OR i.tenant_id = $1)
        WHERE (wc.tenant_id IS NULL OR wc.tenant_id = $1)
    """
    if not include_archived:
        query += " AND wc.is_active = TRUE"
    if search and search.strip():
        params.append(f"%{normalize_warehouse_category_name(search)}%")
        query += f" AND wc.normalized_name LIKE ${len(params)}"
    params.append(limit)
    query += f"""
        GROUP BY wc.id
        ORDER BY wc.normalized_name, wc.name
        LIMIT ${len(params)}
    """
    rows = await conn.fetch(query, *params)
    return [_category_dict(row, tenant_id) for row in rows]


async def _load_visible_category(conn, tenant_id: UUID, category_id: UUID):
    row = await conn.fetchrow(
        """
        SELECT
            wc.*,
            COUNT(i.id)::int AS ingredient_count,
            COUNT(i.id) FILTER (WHERE i.tenant_id IS NULL)::int AS global_count,
            COUNT(i.id) FILTER (WHERE i.tenant_id = $1)::int AS tenant_count
        FROM warehouse_categories wc
        LEFT JOIN ingredients i
          ON i.warehouse_category_id = wc.id
         AND i.is_active = TRUE
         AND (i.tenant_id IS NULL OR i.tenant_id = $1)
        WHERE wc.id = $2
          AND (wc.tenant_id IS NULL OR wc.tenant_id = $1)
        GROUP BY wc.id
        """,
        tenant_id,
        category_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Warehouse category not found")
    return row


async def _load_owned_category(conn, tenant_id: UUID, category_id: UUID):
    row = await _load_visible_category(conn, tenant_id, category_id)
    if row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Warehouse category not found")
    return row


async def create_warehouse_category(
    conn,
    tenant_id: UUID,
    name: str,
) -> Dict[str, Any]:
    display_name = clean_warehouse_category_name(name)
    normalized_name = normalize_warehouse_category_name(display_name)
    if not normalized_name:
        raise HTTPException(status_code=422, detail="Warehouse category name cannot be empty")

    duplicate = await conn.fetchrow(
        """
        SELECT id, is_active
        FROM warehouse_categories
        WHERE normalized_name = $2
          AND (tenant_id IS NULL OR tenant_id = $1)
        LIMIT 1
        """,
        tenant_id,
        normalized_name,
    )
    if duplicate:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "warehouse_category_exists",
                "is_archived": not duplicate["is_active"],
                "message": "Ya existe una categoría de bodega con ese nombre.",
            },
        )

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO warehouse_categories (tenant_id, name, normalized_name)
            VALUES ($1, $2, $3)
            RETURNING *
            """,
            tenant_id,
            display_name,
            normalized_name,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una categoría de bodega con ese nombre.",
        )
    await record_module_event(
        conn,
        tenant_id,
        domain=DOMAIN_ABASTECIMIENTO,
        action="warehouse_category_created",
        entity_type="warehouse_category",
        entity_id=row["id"],
        label=display_name,
    )
    return _category_dict(row, tenant_id)


async def rename_warehouse_category(
    conn,
    tenant_id: UUID,
    category_id: UUID,
    name: str,
) -> Dict[str, Any]:
    await _load_owned_category(conn, tenant_id, category_id)
    display_name = clean_warehouse_category_name(name)
    normalized_name = normalize_warehouse_category_name(display_name)
    if not normalized_name:
        raise HTTPException(status_code=422, detail="Warehouse category name cannot be empty")

    duplicate = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1
            FROM warehouse_categories
            WHERE id <> $3
              AND normalized_name = $2
              AND (tenant_id IS NULL OR tenant_id = $1)
        )
        """,
        tenant_id,
        normalized_name,
        category_id,
    )
    if duplicate:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una categoría de bodega con ese nombre.",
        )

    try:
        await conn.execute(
            """
            UPDATE warehouse_categories
            SET name = $3, normalized_name = $4, updated_at = NOW()
            WHERE id = $2 AND tenant_id = $1
            """,
            tenant_id,
            category_id,
            display_name,
            normalized_name,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una categoría de bodega con ese nombre.",
        )
    await record_module_event(
        conn,
        tenant_id,
        domain=DOMAIN_ABASTECIMIENTO,
        action="warehouse_category_updated",
        entity_type="warehouse_category",
        entity_id=category_id,
        label=display_name,
    )
    return _category_dict(
        await _load_visible_category(conn, tenant_id, category_id),
        tenant_id,
    )


async def archive_warehouse_category(
    conn,
    tenant_id: UUID,
    category_id: UUID,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    await _load_owned_category(conn, tenant_id, category_id)
    await conn.execute(
        """
        UPDATE warehouse_categories
        SET is_active = FALSE, updated_at = NOW()
        WHERE id = $2 AND tenant_id = $1
        """,
        tenant_id,
        category_id,
    )
    await record_module_event(
        conn,
        tenant_id,
        domain=DOMAIN_ABASTECIMIENTO,
        action="warehouse_category_archived",
        entity_type="warehouse_category",
        entity_id=category_id,
        reason=reason,
    )
    return _category_dict(
        await _load_visible_category(conn, tenant_id, category_id),
        tenant_id,
    )


async def resolve_assignable_warehouse_category(
    conn,
    tenant_id: UUID,
    category_id: Optional[UUID] = None,
    legacy_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if category_id:
        row = await conn.fetchrow(
            """
            SELECT id, name, tenant_id, is_active
            FROM warehouse_categories
            WHERE id = $2
              AND (tenant_id IS NULL OR tenant_id = $1)
            """,
            tenant_id,
            category_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Warehouse category not found")
        if not row["is_active"]:
            raise HTTPException(status_code=422, detail="Warehouse category is archived")
        return dict(row)

    if legacy_name is None or not legacy_name.strip():
        return None

    normalized_name = normalize_warehouse_category_name(legacy_name)
    row = await conn.fetchrow(
        """
        SELECT id, name, tenant_id, is_active
        FROM warehouse_categories
        WHERE normalized_name = $2
          AND (tenant_id IS NULL OR tenant_id = $1)
        ORDER BY tenant_id NULLS FIRST
        LIMIT 1
        """,
        tenant_id,
        normalized_name,
    )
    if row:
        if not row["is_active"]:
            raise HTTPException(status_code=422, detail="Warehouse category is archived")
        return dict(row)

    created = await create_warehouse_category(conn, tenant_id, legacy_name)
    return {
        "id": created["id"],
        "name": created["name"],
        "tenant_id": created["tenant_id"],
        "is_active": created["is_active"],
    }


async def resolve_global_warehouse_category(
    conn,
    name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not name or not name.strip():
        return None
    display_name = clean_warehouse_category_name(name)
    normalized_name = normalize_warehouse_category_name(display_name)
    row = await conn.fetchrow(
        """
        SELECT id, name
        FROM warehouse_categories
        WHERE tenant_id IS NULL AND normalized_name = $1
        """,
        normalized_name,
    )
    if row:
        return dict(row)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO warehouse_categories (tenant_id, name, normalized_name)
            VALUES (NULL, $1, $2)
            RETURNING id, name
            """,
            display_name,
            normalized_name,
        )
    except asyncpg.UniqueViolationError:
        row = await conn.fetchrow(
            """
            SELECT id, name
            FROM warehouse_categories
            WHERE tenant_id IS NULL AND normalized_name = $1
            """,
            normalized_name,
        )
    return dict(row)
