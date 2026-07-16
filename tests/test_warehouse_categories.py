from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.warehouse_categories_service import (
    archive_warehouse_category,
    create_warehouse_category,
    list_warehouse_categories,
    normalize_warehouse_category_name,
    rename_warehouse_category,
    resolve_assignable_warehouse_category,
)


def _category_row(tenant_id, name="Lácteos"):
    return {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "name": name,
        "normalized_name": "lacteos",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def test_normalizes_case_diacritics_and_repeated_spaces():
    assert normalize_warehouse_category_name("  LÁCTEOS   Frescos  ") == "lacteos frescos"


@pytest.mark.asyncio
async def test_create_normalizes_and_scopes_to_tenant():
    tenant_id = uuid4()
    inserted = _category_row(tenant_id, "LÁCTEOS Frescos")
    inserted["normalized_name"] = "lacteos frescos"
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, inserted])

    result = await create_warehouse_category(conn, tenant_id, "  LÁCTEOS   Frescos ")

    assert result["name"] == "LÁCTEOS Frescos"
    assert result["normalized_name"] == "lacteos frescos"
    assert result["scope"] == "tenant"
    assert result["can_manage"] is True
    insert_args = conn.fetchrow.await_args_list[1].args
    assert insert_args[1:] == (tenant_id, "LÁCTEOS Frescos", "lacteos frescos")


@pytest.mark.asyncio
async def test_create_rejects_visible_global_duplicate():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": uuid4(), "is_active": True})

    with pytest.raises(HTTPException) as exc:
        await create_warehouse_category(conn, tenant_id, " lacteos ")

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_assignment_hides_cross_tenant_category():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await resolve_assignable_warehouse_category(
            conn,
            tenant_id,
            category_id=uuid4(),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_assignment_rejects_archived_visible_category():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": uuid4(),
        "name": "Salsas",
        "tenant_id": tenant_id,
        "is_active": False,
    })

    with pytest.raises(HTTPException) as exc:
        await resolve_assignable_warehouse_category(
            conn,
            tenant_id,
            category_id=uuid4(),
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_list_is_scoped_to_global_or_current_tenant():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    await list_warehouse_categories(conn, tenant_id, search=" Lácteos ", limit=25)

    query, *params = conn.fetch.await_args.args
    assert "(wc.tenant_id IS NULL OR wc.tenant_id = $1)" in query
    assert "wc.is_active = TRUE" in query
    assert params == [tenant_id, "%lacteos%", 25]


@pytest.mark.asyncio
async def test_tenant_cannot_rename_global_category():
    tenant_id = uuid4()
    global_row = _category_row(None)
    global_row.update({
        "ingredient_count": 1,
        "global_count": 1,
        "tenant_count": 0,
    })
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=global_row)

    with pytest.raises(HTTPException) as exc:
        await rename_warehouse_category(
            conn,
            tenant_id,
            global_row["id"],
            "Nuevo nombre",
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_rename_keeps_category_identity():
    tenant_id = uuid4()
    category_id = uuid4()
    owned = _category_row(tenant_id)
    owned["id"] = category_id
    owned.update({
        "ingredient_count": 2,
        "global_count": 0,
        "tenant_count": 2,
    })
    renamed = dict(owned, name="Lácteos refrigerados", normalized_name="lacteos refrigerados")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[owned, renamed])
    conn.fetchval = AsyncMock(return_value=False)

    result = await rename_warehouse_category(
        conn,
        tenant_id,
        category_id,
        " Lácteos   refrigerados ",
    )

    assert result["name"] == "Lácteos refrigerados"
    update_query, *update_params = conn.execute.await_args.args
    assert "UPDATE warehouse_categories" in update_query
    assert update_params == [
        tenant_id,
        category_id,
        "Lácteos refrigerados",
        "lacteos refrigerados",
    ]


@pytest.mark.asyncio
async def test_archive_keeps_existing_association_counts():
    tenant_id = uuid4()
    active = _category_row(tenant_id)
    active.update({
        "ingredient_count": 3,
        "global_count": 0,
        "tenant_count": 3,
    })
    archived = dict(active, is_active=False)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[active, archived])

    result = await archive_warehouse_category(conn, tenant_id, active["id"])

    assert result["is_active"] is False
    assert result["ingredient_count"] == 3
