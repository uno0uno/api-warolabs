"""
Comandas Service — KDS (Kitchen Display System)

Core engine: fire_comandas() groups unfired order items by kitchen station
and atomically creates comandas + comanda_items, marking items as 'sent'.

Industry reference: Toast "Send" button, Square "Fire course".

Issue: https://github.com/uno0uno/warocol.com/issues/413
Issue: https://github.com/uno0uno/warocol.com/issues/416
"""
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime, timezone, timedelta
import json
import logging

from app.database import get_db_connection
from app.services.stations_service import get_effective_station
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError, ValidationError
from fastapi import Request

logger = logging.getLogger(__name__)

# Allowed status transitions — any move not in this map is rejected with 422.
# 'recall' (delivered → ready) is handled by recall_comanda() separately.
ALLOWED_TRANSITIONS: Dict[str, List[str]] = {
    'pending':   ['preparing', 'cancelled'],
    'preparing': ['ready', 'cancelled'],
    'ready':     ['delivered'],
    'delivered': [],
    'cancelled': [],
}


async def _check_comandas_enabled(conn, tenant_id: UUID) -> None:
    """Raises APIError(403) if tenant does not have comandas_enabled = true."""
    enabled = await conn.fetchval(
        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
        tenant_id,
    )
    if not enabled:
        raise APIError("Comandas no están habilitadas para este tenant", status_code=403)


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


def _compute_alert_level(fired_at: Optional[datetime], threshold_1_min: Optional[int], threshold_2_min: Optional[int]) -> int:
    """
    Compute alert level (0=normal, 1=warning, 2=critical) from elapsed time vs thresholds.
    Returns 0 if fired_at is None.
    """
    if not fired_at:
        return 0
    now_utc = datetime.now(timezone.utc)
    # fired_at may be timezone-aware (timestamptz from PG); ensure comparison is valid
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    elapsed = int((now_utc - fired_at).total_seconds())
    t1 = (threshold_1_min or 8) * 60
    t2 = (threshold_2_min or 15) * 60
    if elapsed > t2:
        return 2
    if elapsed > t1:
        return 1
    return 0


async def get_comandas_for_kds(
    request: Request,
    station_id: Optional[UUID] = None,
    status_filter: Optional[str] = None,
    date: Optional[str] = None,
    source_type: Optional[str] = None,
) -> dict:
    """
    Returns active comandas for the tenant, optionally filtered by station, status,
    source_type, and date. Computes elapsed_seconds and alert_level server-side.
    Active statuses default: 'pending', 'preparing', 'ready'.
    Terminal: 'delivered', 'cancelled'.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            # Build WHERE clause dynamically
            where_conditions = ["c.tenant_id = $1"]
            params: List[Any] = [tenant_id]
            param_count = 1

            # Default: active statuses only (override if status_filter provided)
            ACTIVE_STATUSES = {'pending', 'preparing', 'ready'}
            if not status_filter:
                where_conditions.append("c.status IN ('pending', 'preparing', 'ready')")
                resolved_statuses = ACTIVE_STATUSES
            else:
                # Support comma-separated values: "pending,preparing,ready"
                status_list = [s.strip() for s in status_filter.split(',') if s.strip()]
                if len(status_list) > 1:
                    placeholders = ', '.join(
                        f'${param_count + i + 1}' for i in range(len(status_list))
                    )
                    where_conditions.append(f"c.status IN ({placeholders})")
                    params.extend(status_list)
                    param_count += len(status_list)
                else:
                    param_count += 1
                    where_conditions.append(f"c.status = ${param_count}")
                    params.append(status_list[0])
                resolved_statuses = set(status_list)

            if station_id:
                param_count += 1
                where_conditions.append(f"c.station_id = ${param_count}")
                params.append(station_id)

            if source_type:
                param_count += 1
                where_conditions.append(f"c.source_type = ${param_count}")
                params.append(source_type)

            # Date filter logic:
            # - Active statuses (pending/preparing/ready): NEVER add date filter — active
            #   orders must always show regardless of when they were fired.
            # - Terminal statuses (delivered/cancelled): default to today in Bogotá time
            #   to prevent the list growing unbounded.
            # - Mixed query (both active + terminal): apply date filter ONLY to terminal
            #   statuses so active ones are never hidden.
            TERMINAL_STATUSES = {'delivered', 'cancelled'}
            has_active = bool(resolved_statuses & ACTIVE_STATUSES)
            has_terminal = bool(resolved_statuses & TERMINAL_STATUSES)

            if date:
                param_count += 1
                where_conditions.append(
                    f"DATE(c.fired_at AT TIME ZONE 'UTC') = ${param_count}::date"
                )
                params.append(date)
            elif has_terminal and not has_active:
                # Only terminal statuses — apply date filter to all
                where_conditions.append(
                    "DATE(c.fired_at AT TIME ZONE 'America/Bogota') = "
                    "(CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')::date"
                )
            elif has_terminal and has_active:
                # Mixed: active statuses always visible; terminal only today
                where_conditions.append(
                    "(c.status IN ('pending', 'preparing', 'ready') OR "
                    "DATE(c.fired_at AT TIME ZONE 'America/Bogota') = "
                    "(CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')::date)"
                )

            where_clause = " AND ".join(where_conditions)

            rows = await conn.fetch(f"""
                SELECT
                    c.id, c.comanda_number, c.status, c.source_type, c.table_display_name,
                    c.notes, c.fired_at, c.preparing_at, c.ready_at, c.delivered_at, c.created_at,
                    ks.id as station_id,
                    ks.name as station_name, ks.kitchen_name as station_kitchen_name,
                    ks.color as station_color,
                    ks.alert_threshold_1_min, ks.alert_threshold_2_min
                FROM comandas c
                JOIN kitchen_stations ks ON ks.id = c.station_id
                WHERE {where_clause}
                ORDER BY c.fired_at ASC
            """, *params)

            comandas = []
            for row in rows:
                c_data = dict(row)

                # Compute elapsed_seconds and alert_level server-side
                elapsed_seconds = None
                if c_data.get('fired_at'):
                    fired_at = c_data['fired_at']
                    if fired_at.tzinfo is None:
                        fired_at = fired_at.replace(tzinfo=timezone.utc)
                    elapsed_seconds = int((datetime.now(timezone.utc) - fired_at).total_seconds())

                c_data['elapsed_seconds'] = elapsed_seconds
                c_data['alert_level'] = _compute_alert_level(
                    c_data.get('fired_at'),
                    c_data.get('alert_threshold_1_min'),
                    c_data.get('alert_threshold_2_min'),
                )

                # Remove raw threshold fields — not needed in response
                c_data.pop('alert_threshold_1_min', None)
                c_data.pop('alert_threshold_2_min', None)

                # Fetch items for this comanda
                item_rows = await conn.fetch("""
                    SELECT id, order_item_id, kitchen_name, quantity, notes,
                           modifiers_snapshot, status, ready_at, created_at
                    FROM comanda_items
                    WHERE comanda_id = $1
                    ORDER BY created_at ASC
                """, row['id'])

                c_data['items'] = [dict(ir) for ir in item_rows]
                comandas.append(c_data)

            return {"success": True, "data": comandas}

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching active comandas: {str(e)}")
        raise APIError(f"Error al obtener comandas del KDS: {str(e)}", status_code=500)


async def get_comanda_detail(
    request: Request,
    comanda_id: UUID,
) -> dict:
    """
    Returns full detail for a single comanda, including nested items,
    station info, timing, elapsed_seconds, and alert_level.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            row = await conn.fetchrow("""
                SELECT
                    c.id, c.comanda_number, c.status, c.source_type, c.table_display_name,
                    c.notes, c.fired_at, c.preparing_at, c.ready_at, c.delivered_at, c.created_at,
                    ks.id as station_id,
                    ks.name as station_name, ks.kitchen_name as station_kitchen_name,
                    ks.color as station_color,
                    ks.alert_threshold_1_min, ks.alert_threshold_2_min
                FROM comandas c
                JOIN kitchen_stations ks ON ks.id = c.station_id
                WHERE c.id = $1 AND c.tenant_id = $2
            """, comanda_id, tenant_id)

            if not row:
                raise NotFoundError(f"Comanda {comanda_id} no encontrada")

            c_data = dict(row)

            # Compute elapsed_seconds and alert_level
            elapsed_seconds = None
            if c_data.get('fired_at'):
                fired_at = c_data['fired_at']
                if fired_at.tzinfo is None:
                    fired_at = fired_at.replace(tzinfo=timezone.utc)
                elapsed_seconds = int((datetime.now(timezone.utc) - fired_at).total_seconds())

            c_data['elapsed_seconds'] = elapsed_seconds
            c_data['alert_level'] = _compute_alert_level(
                c_data.get('fired_at'),
                c_data.get('alert_threshold_1_min'),
                c_data.get('alert_threshold_2_min'),
            )
            c_data.pop('alert_threshold_1_min', None)
            c_data.pop('alert_threshold_2_min', None)

            # Fetch nested items
            item_rows = await conn.fetch("""
                SELECT id, order_item_id, kitchen_name, quantity, notes,
                       modifiers_snapshot, status, ready_at, created_at
                FROM comanda_items
                WHERE comanda_id = $1
                ORDER BY created_at ASC
            """, comanda_id)

            c_data['items'] = [dict(ir) for ir in item_rows]

            return {"success": True, "data": c_data}

    except (AuthenticationError, NotFoundError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching comanda detail {comanda_id}: {str(e)}")
        raise APIError(f"Error al obtener detalle de comanda: {str(e)}", status_code=500)


async def update_comanda_status(
    request: Request,
    comanda_id: UUID,
    new_status: str
) -> dict:
    """
    Updates comanda status and sets appropriate timestamps.
    Enforces allowed-transition map — raises 422 for illegal transitions.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            # Fetch current comanda (verify ownership)
            row = await conn.fetchrow(
                "SELECT id, status FROM comandas WHERE id = $1 AND tenant_id = $2",
                comanda_id, tenant_id
            )
            if not row:
                raise NotFoundError(f"Comanda {comanda_id} no encontrada")

            current_status = row['status']
            allowed_next = ALLOWED_TRANSITIONS.get(current_status, [])

            if new_status not in allowed_next:
                raise ValidationError(
                    f"Transición inválida: {current_status} → {new_status}. "
                    f"Transiciones permitidas desde '{current_status}': {allowed_next}"
                )

            sql_updates = ["status = $1", "updated_at = NOW()"]
            params: List[Any] = [new_status, comanda_id, tenant_id]

            if new_status == 'preparing':
                sql_updates.append("preparing_at = NOW()")
            elif new_status == 'ready':
                sql_updates.append("ready_at = NOW()")
            elif new_status == 'delivered':
                sql_updates.append("delivered_at = NOW()")

            await conn.execute(f"""
                UPDATE comandas
                SET {', '.join(sql_updates)}
                WHERE id = $2 AND tenant_id = $3
            """, *params)

            # Propagate 'preparing' back to order_items so POS badge updates
            if new_status == 'preparing':
                await conn.execute("""
                    UPDATE order_items oi
                    SET fulfillment_status = 'preparing'
                    FROM comanda_items ci
                    WHERE ci.comanda_id = $1
                      AND ci.order_item_id = oi.id
                      AND ci.status != 'cancelled'
                      AND oi.fulfillment_status = 'sent'
                """, comanda_id)

            return {"success": True, "message": f"Comanda actualizada a {new_status}"}

    except (AuthenticationError, NotFoundError, ValidationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating comanda status: {str(e)}")
        raise APIError(f"Error al actualizar comanda: {str(e)}", status_code=500)


async def bulk_update_comanda_status(
    request: Request,
    comanda_ids: List[UUID],
    new_status: str,
) -> dict:
    """
    Bulk status update for multiple comandas.
    Applies the same ALLOWED_TRANSITIONS rules per comanda.
    Skips (with reason) any comanda whose current state doesn't allow the transition.
    Returns counts of updated and skipped comandas.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            rows = await conn.fetch(
                "SELECT id, status FROM comandas WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
                comanda_ids, tenant_id,
            )

            found_ids = {row['id'] for row in rows}
            updated, skipped = 0, 0

            for row in rows:
                current_status = row['status']
                allowed_next = ALLOWED_TRANSITIONS.get(current_status, [])
                if new_status not in allowed_next:
                    skipped += 1
                    continue

                sql_updates = ["status = $1", "updated_at = NOW()"]
                params: List[Any] = [new_status, row['id'], tenant_id]

                if new_status == 'preparing':
                    sql_updates.append("preparing_at = NOW()")
                elif new_status == 'ready':
                    sql_updates.append("ready_at = NOW()")
                elif new_status == 'delivered':
                    sql_updates.append("delivered_at = NOW()")

                await conn.execute(
                    f"UPDATE comandas SET {', '.join(sql_updates)} WHERE id = $2 AND tenant_id = $3",
                    *params,
                )

                if new_status == 'preparing':
                    await conn.execute("""
                        UPDATE order_items oi
                        SET fulfillment_status = 'preparing'
                        FROM comanda_items ci
                        WHERE ci.comanda_id = $1
                          AND ci.order_item_id = oi.id
                          AND ci.status != 'cancelled'
                          AND oi.fulfillment_status = 'sent'
                    """, row['id'])

                updated += 1

            not_found = len(comanda_ids) - len(found_ids)

            return {
                "success": True,
                "updated": updated,
                "skipped": skipped + not_found,
                "message": f"{updated} comanda(s) actualizadas a '{new_status}'",
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error in bulk_update_comanda_status: {str(e)}")
        raise APIError(f"Error al actualizar comandas: {str(e)}", status_code=500)


async def recall_comanda(
    request: Request,
    comanda_id: UUID
) -> dict:
    """Reverts a 'delivered' comanda back to 'ready' (15 min window)."""
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            row = await conn.fetchrow("""
                SELECT id, status, delivered_at
                FROM comandas
                WHERE id = $1 AND tenant_id = $2
            """, comanda_id, tenant_id)

            if not row:
                raise NotFoundError(f"Comanda {comanda_id} no encontrada")
            if row['status'] != 'delivered':
                raise ValidationError("Solo se pueden recuperar comandas que ya fueron entregadas")

            # 15-minute window check
            if row['delivered_at'] and (datetime.now(timezone.utc) - row['delivered_at']) > timedelta(minutes=15):
                raise ValidationError("La ventana de recuperación de 15 minutos ha expirado")

            await conn.execute("""
                UPDATE comandas
                SET status = 'ready', delivered_at = NULL, updated_at = NOW()
                WHERE id = $1 AND tenant_id = $2
            """, comanda_id, tenant_id)

            return {"success": True, "message": "Comanda recuperada y marcada como lista"}

    except (AuthenticationError, NotFoundError, ValidationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error recalling comanda {comanda_id}: {str(e)}")
        raise APIError(f"Error al recuperar comanda: {str(e)}", status_code=500)


async def update_comanda_item_status(
    request: Request,
    comanda_id: UUID,
    item_id: UUID,
    new_status: str
) -> dict:
    """
    Updates individual comanda item status.

    Allowed transitions:
    - pending/ready → 'ready'     (kitchen marks item done)
    - any non-terminal → 'cancelled'  (item voided from POS tab)

    Side effects (atomic, single transaction):
    For 'ready':
      1. Sets comanda_items.status = 'ready', ready_at = now()
      2. Sets order_items.fulfillment_status = 'ready', ready_at = now()
      3. If ALL non-cancelled items are now 'ready' → auto-advances comanda to 'ready'
    For 'cancelled':
      1. Sets comanda_items.status = 'cancelled', cancelled_at = now()
      2. Sets order_items.fulfillment_status = 'cancelled' (if row still exists)
      3. If ALL items are now 'cancelled' → auto-cancels comanda
    """
    allowed = {'ready', 'cancelled'}
    if new_status not in allowed:
        raise ValidationError(
            f"Estado de ítem inválido: {new_status}. Permitidos: {allowed}"
        )

    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            # Verify item belongs to this comanda and tenant owns it
            item_row = await conn.fetchrow("""
                SELECT ci.id, ci.comanda_id, ci.order_item_id, ci.status AS current_status
                FROM comanda_items ci
                JOIN comandas c ON c.id = ci.comanda_id
                WHERE ci.id = $1
                  AND ci.comanda_id = $2
                  AND c.tenant_id = $3
            """, item_id, comanda_id, tenant_id)

            if not item_row:
                raise NotFoundError(
                    f"Ítem {item_id} no encontrado en comanda {comanda_id}"
                )

            order_item_id = item_row['order_item_id']
            comanda_auto_updated = False

            async with conn.transaction():
                if new_status == 'ready':
                    # 1. Mark item ready
                    await conn.execute("""
                        UPDATE comanda_items
                        SET status = 'ready', ready_at = now()
                        WHERE id = $1
                    """, item_id)

                    # 2. Mirror on order_item
                    await conn.execute("""
                        UPDATE order_items
                        SET fulfillment_status = 'ready', ready_at = now()
                        WHERE id = $1
                    """, order_item_id)

                    # 3. Auto-advance comanda if all non-cancelled items are ready
                    non_ready_count = await conn.fetchval("""
                        SELECT COUNT(*)
                        FROM comanda_items
                        WHERE comanda_id = $1
                          AND status NOT IN ('ready', 'cancelled')
                    """, comanda_id)

                    if non_ready_count == 0:
                        await conn.execute("""
                            UPDATE comandas
                            SET status = 'ready', ready_at = now(), updated_at = now()
                            WHERE id = $1 AND tenant_id = $2
                              AND status NOT IN ('delivered', 'cancelled')
                        """, comanda_id, tenant_id)
                        comanda_auto_updated = True
                        logger.info(
                            f"update_comanda_item_status: all active items ready — "
                            f"auto-advanced comanda {comanda_id} to 'ready'"
                        )

                else:  # cancelled
                    # 1. Mark item cancelled
                    await conn.execute("""
                        UPDATE comanda_items
                        SET status = 'cancelled', cancelled_at = now()
                        WHERE id = $1
                    """, item_id)

                    # 2. Mirror on order_item (may already be deleted — ignore if gone)
                    await conn.execute("""
                        UPDATE order_items
                        SET fulfillment_status = 'cancelled'
                        WHERE id = $1
                    """, order_item_id)

                    # 3. Auto-cancel comanda if ALL items are now cancelled
                    active_count = await conn.fetchval("""
                        SELECT COUNT(*)
                        FROM comanda_items
                        WHERE comanda_id = $1
                          AND status != 'cancelled'
                    """, comanda_id)

                    if active_count == 0:
                        await conn.execute("""
                            UPDATE comandas
                            SET status = 'cancelled', updated_at = now()
                            WHERE id = $1 AND tenant_id = $2
                              AND status NOT IN ('delivered', 'cancelled')
                        """, comanda_id, tenant_id)
                        comanda_auto_updated = True
                        logger.info(
                            f"update_comanda_item_status: all items cancelled — "
                            f"auto-cancelled comanda {comanda_id}"
                        )

            return {
                "success": True,
                "message": f"Ítem actualizado a {new_status}",
                "comanda_auto_updated": comanda_auto_updated,
            }

    except (AuthenticationError, NotFoundError, ValidationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating comanda item status: {str(e)}")
        raise APIError(f"Error al actualizar ítem de comanda: {str(e)}", status_code=500)


async def get_comanda_history(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    station_id: Optional[UUID] = None,
    status_filter: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> dict:
    """
    Returns paginated history of all comandas for the tenant.
    Includes delivered and cancelled orders.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            where_conditions = ["c.tenant_id = $1"]
            params: List[Any] = [tenant_id]
            param_count = 1

            if date_from:
                param_count += 1
                where_conditions.append(f"c.fired_at >= ${param_count}::timestamptz")
                params.append(date_from)
            if date_to:
                param_count += 1
                where_conditions.append(f"c.fired_at <= ${param_count}::timestamptz")
                params.append(date_to)
            if station_id:
                param_count += 1
                where_conditions.append(f"c.station_id = ${param_count}")
                params.append(station_id)
            if status_filter:
                param_count += 1
                where_conditions.append(f"c.status = ${param_count}")
                params.append(status_filter)
            if source_type:
                param_count += 1
                where_conditions.append(f"c.source_type = ${param_count}")
                params.append(source_type)

            where_clause = " AND ".join(where_conditions)

            # Count total
            total_count = await conn.fetchval(
                f"SELECT COUNT(*) FROM comandas c WHERE {where_clause}", *params
            )

            # Fetch rows
            rows = await conn.fetch(f"""
                SELECT
                    c.id, c.comanda_number, c.status, c.source_type, c.table_display_name,
                    c.notes, c.fired_at, c.preparing_at, c.ready_at, c.delivered_at, c.created_at,
                    ks.name as station_name
                FROM comandas c
                JOIN kitchen_stations ks ON ks.id = c.station_id
                WHERE {where_clause}
                ORDER BY c.fired_at DESC
                LIMIT ${param_count + 1} OFFSET ${param_count + 2}
            """, *params, limit, offset)

            comandas = []
            for row in rows:
                c_data = dict(row)
                comandas.append(c_data)

            return {
                "success": True,
                "data": comandas,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + limit) < total_count
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching comanda history: {str(e)}")
        raise APIError(f"Error al obtener historial de comandas: {str(e)}", status_code=500)


async def get_comanda_summary(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> dict:
    """
    Returns aggregated daily stats per kitchen station.
    Calculates avg prep time (fired_at -> ready_at).
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        async with get_db_connection() as conn:
            where_conditions = ["c.tenant_id = $1"]
            params: List[Any] = [tenant_id]
            param_count = 1

            if date_from:
                param_count += 1
                where_conditions.append(f"c.fired_at >= ${param_count}::timestamptz")
                params.append(date_from)
            if date_to:
                param_count += 1
                where_conditions.append(f"c.fired_at <= ${param_count}::timestamptz")
                params.append(date_to)

            where_clause = " AND ".join(where_conditions)

            # Aggregate stats
            stats = await conn.fetch(f"""
                SELECT
                    ks.id as station_id,
                    ks.name as station_name,
                    ks.kitchen_name as station_short_name,
                    COUNT(c.id) as total_comandas,
                    AVG(EXTRACT(EPOCH FROM (c.ready_at - c.fired_at))) FILTER (WHERE c.ready_at IS NOT NULL) as avg_prep_time_seconds,
                    COUNT(c.id) FILTER (WHERE c.status = 'delivered') as delivered_count
                FROM kitchen_stations ks
                LEFT JOIN comandas c ON c.station_id = ks.id AND {where_clause}
                WHERE ks.tenant_id = $1
                GROUP BY ks.id, ks.name, ks.kitchen_name
                ORDER BY ks.display_order ASC
            """, *params)

            return {
                "success": True,
                "data": [
                    {
                        "station_id": str(s['station_id']),
                        "station_name": s['station_name'],
                        "station_short_name": s['station_short_name'],
                        "total_comandas": s['total_comandas'],
                        "delivered_count": s['delivered_count'],
                        "avg_prep_time_seconds": round(float(s['avg_prep_time_seconds']), 1) if s['avg_prep_time_seconds'] else None
                    }
                    for s in stats
                ]
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching comanda summary: {str(e)}")
        raise APIError(f"Error al obtener resumen de cocina: {str(e)}", status_code=500)


async def get_daily_stats(
    request: Request,
    date: Optional[str] = None,
    station_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    Returns daily performance stats per kitchen station for a single date.
    Includes total/delivered/cancelled counts, avg prep time, delay counts
    (vs per-station thresholds), and source breakdown.

    Issue: https://github.com/uno0uno/warocol.com/issues/424
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            await _check_comandas_enabled(conn, tenant_id)

            date_str = date if date else datetime.now(timezone.utc).date().isoformat()

            rows = await conn.fetch(
                """
                SELECT
                    ks.id AS station_id,
                    ks.name AS station_name,
                    ks.color AS station_color,
                    ks.kitchen_name AS station_kitchen_name,
                    COUNT(c.id) AS total_count,
                    COUNT(c.id) FILTER (WHERE c.status = 'delivered') AS delivered_count,
                    COUNT(c.id) FILTER (WHERE c.status = 'cancelled') AS cancelled_count,
                    AVG(EXTRACT(EPOCH FROM (c.ready_at - c.fired_at))) FILTER (WHERE c.ready_at IS NOT NULL) AS avg_prep_time_seconds,
                    COUNT(c.id) FILTER (
                        WHERE c.ready_at IS NULL
                        AND EXTRACT(EPOCH FROM (NOW() - c.fired_at)) / 60 > ks.alert_threshold_1_min
                    ) + COUNT(c.id) FILTER (
                        WHERE c.ready_at IS NOT NULL
                        AND EXTRACT(EPOCH FROM (c.ready_at - c.fired_at)) / 60 > ks.alert_threshold_1_min
                    ) AS delayed_count,
                    COUNT(c.id) FILTER (
                        WHERE c.ready_at IS NULL
                        AND EXTRACT(EPOCH FROM (NOW() - c.fired_at)) / 60 > ks.alert_threshold_2_min
                    ) + COUNT(c.id) FILTER (
                        WHERE c.ready_at IS NOT NULL
                        AND EXTRACT(EPOCH FROM (c.ready_at - c.fired_at)) / 60 > ks.alert_threshold_2_min
                    ) AS very_delayed_count,
                    COUNT(c.id) FILTER (WHERE c.source_type = 'table') AS source_table,
                    COUNT(c.id) FILTER (WHERE c.source_type = 'pos') AS source_pos,
                    COUNT(c.id) FILTER (WHERE c.source_type = 'delivery') AS source_delivery,
                    COUNT(c.id) FILTER (WHERE c.source_type = 'pickup') AS source_pickup
                FROM kitchen_stations ks
                LEFT JOIN comandas c ON c.station_id = ks.id
                    AND c.tenant_id = $1
                    AND DATE(c.fired_at) = $2::date
                WHERE ks.tenant_id = $1
                    AND ks.is_active = TRUE
                    AND ($3::uuid IS NULL OR ks.id = $3)
                GROUP BY ks.id, ks.name, ks.color, ks.kitchen_name, ks.alert_threshold_1_min, ks.alert_threshold_2_min
                ORDER BY ks.display_order
                """,
                tenant_id, date_str, station_id,
            )

            return {
                "date": date_str,
                "stations": [
                    {
                        "station": {
                            "id": str(row["station_id"]),
                            "name": row["station_name"],
                            "color": row["station_color"],
                            "kitchen_name": row["station_kitchen_name"],
                        },
                        "total_count": row["total_count"] or 0,
                        "delivered_count": row["delivered_count"] or 0,
                        "cancelled_count": row["cancelled_count"] or 0,
                        "avg_prep_time_seconds": round(float(row["avg_prep_time_seconds"]), 1) if row["avg_prep_time_seconds"] else None,
                        "delayed_count": row["delayed_count"] or 0,
                        "very_delayed_count": row["very_delayed_count"] or 0,
                        "by_source": {
                            "table": row["source_table"] or 0,
                            "pos": row["source_pos"] or 0,
                            "delivery": row["source_delivery"] or 0,
                            "pickup": row["source_pickup"] or 0,
                        },
                    }
                    for row in rows
                ],
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching daily stats: {str(e)}")
        raise APIError(f"Error al obtener estadísticas diarias: {str(e)}", status_code=500)
