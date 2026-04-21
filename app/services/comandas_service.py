"""
Comandas Service — KDS (Kitchen Display System)

Core engine: fire_comandas() groups unfired order items by kitchen station
and atomically creates comandas + comanda_items, marking items as 'sent'.

Industry reference: Toast "Send" button, Square "Fire course".

Issue: https://github.com/uno0uno/warocol.com/issues/413
"""
from typing import Optional, List, Dict, Any
from uuid import UUID
import json
import logging

from app.database import get_db_connection
from app.services.stations_service import get_effective_station

logger = logging.getLogger(__name__)


async def fire_comandas(
    order_id: UUID,
    tenant_id: UUID,
    source_type: str,
    table_display_name: str,
    item_ids: Optional[List[UUID]] = None,
    conn=None,
) -> List[Dict[str, Any]]:
    """
    Core KDS engine — delta-send pattern.

    Loads order_items with fulfillment_status='new' (or filtered by item_ids),
    resolves effective kitchen station per item (tier-1: product.station_id →
    tier-2: tenant_category_stations), groups by station, and for each group:
      - Generates comanda_number (sequential per day per station)
      - INSERTs comanda + comanda_items (modifiers snapshot)
      - UPDATEs order_items.fulfillment_status = 'sent'

    Returns list of created comanda dicts (each with nested items list).
    Returns [] if all items have station_id = NULL (no-op).

    Args:
        order_id: UUID of the order to fire
        tenant_id: UUID of the tenant (used for station routing isolation)
        source_type: 'table' | 'pos' | 'delivery' | 'pickup'
        table_display_name: display label, e.g. "Mesa 5", "Domicilio #142", "POS"
        item_ids: if provided, only fire these specific order_item UUIDs
        conn: optional asyncpg connection — pass when already inside a transaction
              to avoid nested BEGIN errors; if None a new connection+transaction is created
    """
    if conn is not None:
        return await _fire_with_conn(
            conn, order_id, tenant_id, source_type, table_display_name, item_ids
        )

    async with get_db_connection() as new_conn:
        async with new_conn.transaction():
            return await _fire_with_conn(
                new_conn, order_id, tenant_id, source_type, table_display_name, item_ids
            )


async def _fire_with_conn(
    conn,
    order_id: UUID,
    tenant_id: UUID,
    source_type: str,
    table_display_name: str,
    item_ids: Optional[List[UUID]],
) -> List[Dict[str, Any]]:
    """Internal implementation — always called with an active connection."""

    # ─── Step 1: Load unfired items ─────────────────────────────────────────
    if item_ids:
        rows = await conn.fetch(
            """
            SELECT
                oi.id,
                oi.quantity,
                oi.product_id,
                COALESCE(p.kitchen_name, p.name) AS kitchen_name
            FROM order_items oi
            JOIN product p ON oi.product_id = p.id
            WHERE oi.order_id = $1
              AND oi.fulfillment_status = 'new'
              AND oi.id = ANY($2::uuid[])
            ORDER BY oi.created_at
            """,
            order_id, item_ids,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT
                oi.id,
                oi.quantity,
                oi.product_id,
                COALESCE(p.kitchen_name, p.name) AS kitchen_name
            FROM order_items oi
            JOIN product p ON oi.product_id = p.id
            WHERE oi.order_id = $1
              AND oi.fulfillment_status = 'new'
            ORDER BY oi.created_at
            """,
            order_id,
        )

    if not rows:
        logger.info(f"fire_comandas: no 'new' items found for order {order_id}")
        return []

    # ─── Step 2: Resolve effective station per item ──────────────────────────
    # get_effective_station() uses the shared conn to run inside our transaction
    items_by_station: Dict[UUID, List[Any]] = {}
    skipped = 0

    for row in rows:
        station_id = await get_effective_station(row['product_id'], tenant_id, conn)
        if station_id is None:
            skipped += 1
            continue
        if station_id not in items_by_station:
            items_by_station[station_id] = []
        items_by_station[station_id].append(row)

    if not items_by_station:
        logger.info(
            f"fire_comandas: all {len(rows)} items have no station routing "
            f"for order {order_id} (skipped={skipped})"
        )
        return []

    # ─── Step 3: Create one comanda per station ──────────────────────────────
    created_comandas: List[Dict[str, Any]] = []

    for station_id, station_items in items_by_station.items():

        # 3a. Generate comanda_number (per day per station, reset at midnight)
        comanda_number = await conn.fetchval(
            """
            SELECT COALESCE(MAX(comanda_number), 0) + 1
            FROM comandas
            WHERE station_id = $1
              AND tenant_id = $2
              AND DATE(fired_at) = CURRENT_DATE
            """,
            station_id, tenant_id,
        )

        # 3b. INSERT comanda
        comanda_row = await conn.fetchrow(
            """
            INSERT INTO comandas (
                tenant_id, order_id, station_id,
                comanda_number, status, source_type, table_display_name
            )
            VALUES ($1, $2, $3, $4, 'pending', $5, $6)
            RETURNING id, comanda_number, station_id, status, source_type,
                      table_display_name, notes, fired_at, ready_at, created_at
            """,
            tenant_id, order_id, station_id,
            comanda_number, source_type, table_display_name,
        )
        comanda_id = comanda_row['id']

        # 3c. INSERT comanda_items — one per order_item in this station's group
        inserted_items: List[Dict[str, Any]] = []
        item_ids_in_group: List[UUID] = []

        for item in station_items:
            order_item_id = item['id']
            item_ids_in_group.append(order_item_id)

            # Build modifiers_snapshot from order_item_modifiers
            mod_rows = await conn.fetch(
                """
                SELECT modifier_name, price_at_purchase
                FROM order_item_modifiers
                WHERE order_item_id = $1
                ORDER BY created_at
                """,
                order_item_id,
            )
            modifiers_snapshot = [
                {"name": m['modifier_name'], "price": float(m['price_at_purchase'])}
                for m in mod_rows
            ] if mod_rows else None

            ci_row = await conn.fetchrow(
                """
                INSERT INTO comanda_items (
                    comanda_id, order_item_id, kitchen_name,
                    quantity, modifiers_snapshot
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, order_item_id, kitchen_name, quantity,
                          notes, modifiers_snapshot, status, ready_at, created_at
                """,
                comanda_id,
                order_item_id,
                item['kitchen_name'],
                item['quantity'],
                json.dumps(modifiers_snapshot) if modifiers_snapshot else None,
            )
            inserted_items.append(dict(ci_row))

        # 3d. Mark fired items as 'sent'
        await conn.execute(
            """
            UPDATE order_items
            SET fulfillment_status = 'sent', sent_at = now()
            WHERE id = ANY($1::uuid[])
            """,
            item_ids_in_group,
        )

        # Fetch station name for response
        station_name = await conn.fetchval(
            "SELECT name FROM kitchen_stations WHERE id = $1",
            station_id,
        )

        comanda_dict = dict(comanda_row)
        comanda_dict['station_name'] = station_name
        comanda_dict['items'] = inserted_items

        created_comandas.append(comanda_dict)
        logger.info(
            f"fire_comandas: comanda #{comanda_number} created "
            f"(station={station_id}, items={len(inserted_items)}, order={order_id})"
        )

    fired_count = sum(len(c['items']) for c in created_comandas)
    logger.info(
        f"fire_comandas complete: order={order_id} "
        f"fired={fired_count} skipped={skipped} "
        f"comandas={len(created_comandas)}"
    )
    return created_comandas
