"""Tests for comanda_fired SSE notify on fire_comandas (warocol.com#1971)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import notifications_service
from app.services.comandas_service import _fire_with_conn


@pytest.mark.asyncio
async def test_notify_comanda_fired_is_sse_only_no_insert():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()
    payload = {
        "order_id": str(uuid4()),
        "source_type": "table",
        "comandas": [{"id": str(uuid4()), "comanda_number": 1, "items": []}],
    }

    await notifications_service.notify_comanda_fired(conn, tenant_id, payload)

    assert conn.execute.await_count == 1
    sql = conn.execute.await_args.args[0]
    assert "pg_notify" in sql
    assert "INSERT INTO notifications" not in sql
    notify_json = conn.execute.await_args.args[2]
    assert '"type": "comanda_fired"' in notify_json or '"type":"comanda_fired"' in notify_json.replace(
        " ", ""
    )


@pytest.mark.asyncio
async def test_notify_comanda_fired_json_safe_uuid_datetime():
    conn = MagicMock()
    conn.execute = AsyncMock()
    cid = uuid4()
    fired = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    await notifications_service.notify_comanda_fired(
        conn,
        uuid4(),
        {
            "order_id": uuid4(),
            "source_type": "pos",
            "comandas": [{"id": cid, "fired_at": fired, "items": []}],
        },
    )

    raw = conn.execute.await_args.args[2]
    assert str(cid) in raw
    assert "2026-07-31T12:00:00" in raw


@pytest.mark.asyncio
async def test_notify_comanda_fired_handles_decimal_in_payload():
    from decimal import Decimal

    conn = MagicMock()
    conn.execute = AsyncMock()
    await notifications_service.notify_comanda_fired(
        conn,
        uuid4(),
        {
            "order_id": str(uuid4()),
            "source_type": "table",
            "auto_print_target": "caja",
            "comandas": [
                {
                    "id": uuid4(),
                    "comanda_number": 1,
                    "items": [
                        {
                            "kitchen_name": "poker",
                            "quantity": Decimal("2"),
                            "notes": None,
                            "modifiers_snapshot": [{"name": "x", "price": Decimal("1000.50")}],
                        }
                    ],
                }
            ],
        },
    )
    raw = conn.execute.await_args.args[2]
    assert "poker" in raw
    assert "1000.5" in raw or "1000.50" in raw


def test_build_comanda_fired_print_payload_strips_decimals():
    from decimal import Decimal

    slim = notifications_service.build_comanda_fired_print_payload(
        [
            {
                "id": uuid4(),
                "comanda_number": 7,
                "station_name": "Barra",
                "items": [
                    {
                        "kitchen_name": "PASSION",
                        "quantity": Decimal("1"),
                        "notes": "CON PEPINILLOS",
                        "modifiers_snapshot": [{"name": "hielo", "quantity": Decimal("1"), "price": Decimal("0")}],
                    }
                ],
            }
        ]
    )
    assert len(slim) == 1
    assert slim[0]["items"][0]["quantity"] == 1.0
    assert slim[0]["items"][0]["notes"] == "CON PEPINILLOS"
    assert slim[0]["items"][0]["modifiers_snapshot"][0]["name"] == "hielo"


@pytest.mark.asyncio
async def test_fire_with_conn_notifies_when_comandas_created():
    """When fire creates comandas, notify_comanda_fired is called once."""
    order_id = uuid4()
    tenant_id = uuid4()
    station_id = uuid4()
    item_id = uuid4()

    conn = MagicMock()
    # rows for new items
    item_row = {
        "id": item_id,
        "product_id": uuid4(),
        "kitchen_name": "Burger",
        "quantity": 1,
        "notes": None,
        "modifiers_snapshot": None,
    }

    async def fetch_side_effect(sql, *args):
        if "fulfillment_status = 'new'" in sql or "fulfillment_status='new'" in sql.replace(" ", ""):
            return [item_row]
        return []

    conn.fetch = AsyncMock(side_effect=fetch_side_effect)
    conn.fetchval = AsyncMock(side_effect=[10, 1, "Cocina"])  # order_number, index, station name
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": uuid4(),
                "comanda_number": 10,
                "comanda_index": 1,
                "station_id": station_id,
                "status": "pending",
                "source_type": "table",
                "table_display_name": "Mesa 1",
                "notes": None,
                "fired_at": datetime.now(timezone.utc),
                "ready_at": None,
                "created_at": datetime.now(timezone.utc),
            },
            {
                "id": uuid4(),
                "order_item_id": item_id,
                "kitchen_name": "Burger",
                "quantity": 1,
                "notes": None,
                "modifiers_snapshot": None,
                "status": "pending",
                "ready_at": None,
                "created_at": datetime.now(timezone.utc),
            },
        ]
    )
    conn.execute = AsyncMock()

    with patch(
        "app.services.comandas_service.get_effective_station",
        new=AsyncMock(return_value=station_id),
    ), patch(
        "app.services.comandas_service._build_comanda_print_items_for_order_item",
        new=AsyncMock(
            return_value=[
                {
                    "quantity": 1,
                    "notes": None,
                    "modifiers_snapshot": None,
                    "is_promo_free": False,
                    "kitchen_name": "Burger",
                }
            ]
        ),
    ), patch(
        "app.services.notifications_service.notify_comanda_fired",
        new_callable=AsyncMock,
    ) as notify:
        result = await _fire_with_conn(
            conn, order_id, tenant_id, "table", "Mesa 1", None
        )

    assert len(result) >= 1
    notify.assert_awaited_once()
    args = notify.await_args.args
    assert args[1] == tenant_id
    assert args[2]["source_type"] == "table"
    assert args[2]["order_id"] == str(order_id)
    assert args[2]["auto_print_target"] == "caja"
    assert len(args[2]["comandas"]) >= 1


@pytest.mark.asyncio
async def test_fire_with_conn_skips_notify_for_delivery():
    order_id = uuid4()
    tenant_id = uuid4()
    station_id = uuid4()
    item_id = uuid4()
    item_row = {
        "id": item_id,
        "product_id": uuid4(),
        "kitchen_name": "Burger",
        "quantity": 1,
        "notes": None,
        "modifiers_snapshot": None,
    }
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[item_row])
    conn.fetchval = AsyncMock(side_effect=[10, 1, "Cocina"])
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": uuid4(),
                "comanda_number": 10,
                "comanda_index": 1,
                "station_id": station_id,
                "status": "pending",
                "source_type": "delivery",
                "table_display_name": "Domicilio #10",
                "notes": None,
                "fired_at": datetime.now(timezone.utc),
                "ready_at": None,
                "created_at": datetime.now(timezone.utc),
            },
            {
                "id": uuid4(),
                "order_item_id": item_id,
                "kitchen_name": "Burger",
                "quantity": 1,
                "notes": None,
                "modifiers_snapshot": None,
                "status": "pending",
                "ready_at": None,
                "created_at": datetime.now(timezone.utc),
            },
        ]
    )
    conn.execute = AsyncMock()

    with patch(
        "app.services.comandas_service.get_effective_station",
        new=AsyncMock(return_value=station_id),
    ), patch(
        "app.services.comandas_service._build_comanda_print_items_for_order_item",
        new=AsyncMock(
            return_value=[
                {
                    "quantity": 1,
                    "notes": None,
                    "modifiers_snapshot": None,
                    "is_promo_free": False,
                    "kitchen_name": "Burger",
                }
            ]
        ),
    ), patch(
        "app.services.notifications_service.notify_comanda_fired",
        new_callable=AsyncMock,
    ) as notify:
        await _fire_with_conn(
            conn, order_id, tenant_id, "delivery", "Domicilio #10", None
        )

    notify.assert_not_awaited()
