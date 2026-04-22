"""
Stations Service
Business logic for kitchen station (preparation point) management.

Issue: https://github.com/uno0uno/warocol.com/issues/411
"""
from typing import Optional, List
from uuid import UUID
from fastapi import Request, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
import logging

logger = logging.getLogger(__name__)


async def list_stations(request: Request) -> dict:
    """Return all stations for the tenant ordered by display_order."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM kitchen_stations
            WHERE tenant_id = $1
            ORDER BY display_order, name
            """,
            tenant_id,
        )
        return {"success": True, "data": [dict(r) for r in rows]}


async def list_active_stations(request: Request) -> dict:
    """Return only active stations for the tenant ordered by display_order."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM kitchen_stations
            WHERE tenant_id = $1 AND is_active = true
            ORDER BY display_order, name
            """,
            tenant_id,
        )
        return {"success": True, "data": [dict(r) for r in rows]}


async def create_station(request: Request, body) -> dict:
    """Create a new kitchen station for the tenant."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO kitchen_stations (
                tenant_id, name, kitchen_name, color,
                alert_threshold_1_min, alert_threshold_2_min, display_order
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            tenant_id,
            body.name,
            body.kitchen_name,
            body.color,
            body.alert_threshold_1_min,
            body.alert_threshold_2_min,
            body.display_order,
        )
        logger.info(f"Created station '{body.name}' for tenant {tenant_id}")
        return {"success": True, "data": dict(row)}


async def update_station(request: Request, station_id: UUID, body) -> dict:
    """Partial-update a station — only fields present in the request body."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    data_dict = body.model_dump(exclude_unset=True)
    if not data_dict:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_fields = []
    params = []
    param_counter = 1

    for field, value in data_dict.items():
        update_fields.append(f"{field} = ${param_counter}")
        params.append(value)
        param_counter += 1

    update_fields.append("updated_at = now()")

    # WHERE clause params
    params.append(tenant_id)
    params.append(station_id)

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE kitchen_stations
            SET {', '.join(update_fields)}
            WHERE tenant_id = ${param_counter} AND id = ${param_counter + 1}
            RETURNING *
            """,
            *params,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Station not found")
        return {"success": True, "data": dict(row)}


async def soft_delete_station(request: Request, station_id: UUID) -> dict:
    """
    Soft-delete a station (is_active = false).
    Returns 409 if the station has active comandas (pending / preparing / ready).
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        # Verify station belongs to this tenant
        exists = await conn.fetchval(
            "SELECT 1 FROM kitchen_stations WHERE id = $1 AND tenant_id = $2",
            station_id, tenant_id,
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Station not found")

        # Block soft-delete if active comandas reference the station
        active_comanda = await conn.fetchval(
            """
            SELECT 1 FROM comandas
            WHERE station_id = $1 AND status IN ('pending', 'preparing', 'ready')
            LIMIT 1
            """,
            station_id,
        )
        if active_comanda:
            raise HTTPException(
                status_code=409,
                detail="No se puede desactivar: la estación tiene comandas activas",
            )

        await conn.execute(
            """
            UPDATE kitchen_stations
            SET is_active = false, updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            station_id, tenant_id,
        )
        logger.info(f"Soft-deleted station {station_id} for tenant {tenant_id}")
        return {"success": True, "message": "Station deactivated"}


async def toggle_station(request: Request, station_id: UUID, is_active: bool) -> dict:
    """Toggle is_active on/off for a kitchen station."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE kitchen_stations
            SET is_active = $1, updated_at = now()
            WHERE id = $2 AND tenant_id = $3
            RETURNING *
            """,
            is_active, station_id, tenant_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Station not found")
        action = "activated" if is_active else "deactivated"
        logger.info(f"Station {station_id} {action} for tenant {tenant_id}")
        return {"success": True, "data": dict(row)}


async def get_category_stations(request: Request) -> dict:
    """Return all category→station assignments for the tenant."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT
                tcs.category_id,
                c.name AS category_name,
                tcs.station_id,
                ks.name AS station_name,
                ks.color AS station_color
            FROM tenant_category_stations tcs
            JOIN categories c ON tcs.category_id = c.id
            JOIN kitchen_stations ks ON tcs.station_id = ks.id
            WHERE tcs.tenant_id = $1
            ORDER BY c.name
            """,
            tenant_id,
        )
        return {"success": True, "data": [dict(r) for r in rows]}


async def set_category_station(request: Request, category_id: UUID, station_id: Optional[UUID]) -> dict:
    """
    Assign a kitchen station to a category for this tenant (UPSERT).
    Pass station_id=None to clear the assignment (calls delete_category_station logic).
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    if station_id is None:
        return await _delete_category_station_conn(tenant_id, category_id, None)

    async with get_db_connection() as conn:
        # Verify station belongs to this tenant
        exists = await conn.fetchval(
            "SELECT 1 FROM kitchen_stations WHERE id = $1 AND tenant_id = $2",
            station_id, tenant_id,
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Station not found")

        row = await conn.fetchrow(
            """
            INSERT INTO tenant_category_stations (tenant_id, category_id, station_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, category_id)
            DO UPDATE SET station_id = EXCLUDED.station_id
            RETURNING *
            """,
            tenant_id, category_id, station_id,
        )
        logger.info(f"Category {category_id} assigned to station {station_id} for tenant {tenant_id}")
        return {"success": True, "data": dict(row)}


async def delete_category_station(request: Request, category_id: UUID) -> dict:
    """Remove the station assignment for a category."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        await conn.execute(
            """
            DELETE FROM tenant_category_stations
            WHERE tenant_id = $1 AND category_id = $2
            """,
            tenant_id, category_id,
        )
        logger.info(f"Category {category_id} station assignment removed for tenant {tenant_id}")
        return {"success": True, "message": "Assignment removed"}


async def _delete_category_station_conn(tenant_id: UUID, category_id: UUID, _conn) -> dict:
    """Internal helper: delete category assignment using an existing or new connection."""
    async with get_db_connection() as conn:
        await conn.execute(
            """
            DELETE FROM tenant_category_stations
            WHERE tenant_id = $1 AND category_id = $2
            """,
            tenant_id, category_id,
        )
        return {"success": True, "message": "Assignment removed"}


async def get_effective_station(product_id: UUID, tenant_id: UUID, conn) -> Optional[UUID]:
    """
    Two-tier station routing cascade:
      Tier 1 — product.station_id (explicit override)
      Tier 2 — tenant_category_stations for the product's category
      Fallback — None (no comanda generated)

    Takes an existing asyncpg connection so it can run inside fire_comandas() transactions.
    """
    row = await conn.fetchrow(
        """
        SELECT p.station_id, p.category_id
        FROM product p
        WHERE p.id = $1 AND p.tenant_id = $2
        """,
        product_id, tenant_id,
    )
    if not row:
        return None

    # Tier 1: explicit product-level override
    if row['station_id']:
        return row['station_id']

    # Tier 2: category-level mapping for this tenant
    category_station = await conn.fetchval(
        """
        SELECT station_id FROM tenant_category_stations
        WHERE tenant_id = $1 AND category_id = $2
        """,
        tenant_id, row['category_id'],
    )
    return category_station  # None if no mapping


async def reorder_stations(request: Request, items: List) -> dict:
    """
    Bulk-update display_order for multiple stations in a single query.
    Validates all IDs belong to the current tenant before executing.
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    if not items:
        raise HTTPException(status_code=400, detail="No items to reorder")

    ids = [item.id for item in items]
    orders = [item.display_order for item in items]

    async with get_db_connection() as conn:
        # Tenant isolation: all submitted IDs must belong to this tenant
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM kitchen_stations WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
            ids, tenant_id,
        )
        if count != len(ids):
            raise HTTPException(
                status_code=403,
                detail="One or more stations do not belong to this tenant",
            )

        # Single-query bulk update via unnest
        await conn.execute(
            """
            UPDATE kitchen_stations
            SET display_order = v.display_order, updated_at = now()
            FROM unnest($1::uuid[], $2::int[]) AS v(id, display_order)
            WHERE kitchen_stations.id = v.id AND kitchen_stations.tenant_id = $3
            """,
            ids, orders, tenant_id,
        )
        return {"success": True, "message": f"Reordered {len(ids)} stations"}
