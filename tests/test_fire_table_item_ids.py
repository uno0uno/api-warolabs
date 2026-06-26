"""POST /tables/{id}/fire with optional item_ids (#753)."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.services import tables_service


@pytest.mark.asyncio
async def test_fire_table_items_passes_item_ids_to_fire_comandas():
    tenant_id = uuid4()
    table_id = uuid4()
    order_id = uuid4()
    item_a = uuid4()
    item_b = uuid4()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"comandas_enabled": True},
        {"id": table_id, "name": "Mesa 1"},
        {"id": uuid4()},
    ])
    mock_conn.fetch = AsyncMock(return_value=[{"id": order_id}])

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    captured_item_ids = []

    async def capture_fire(*args, **kwargs):
        captured_item_ids.append(kwargs.get("item_ids"))
        return [{"items": [{"id": str(item_a)}]}]

    mock_request = MagicMock()
    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch("app.services.tables_service.fire_comandas", side_effect=capture_fire):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id)

        result = await tables_service.fire_table_items(
            mock_request,
            table_id,
            item_ids=[item_a, item_b],
        )

    assert result["success"] is True
    assert captured_item_ids == [[item_a, item_b]]


@pytest.mark.asyncio
async def test_add_tab_items_auto_fires_only_created_order_item_ids():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    order_id = uuid4()
    old_new_item_id = uuid4()
    created_item_id = uuid4()

    mock_conn = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    captured_item_ids = []

    async def capture_fire(_request, _table_id, item_ids=None):
        captured_item_ids.append(item_ids)
        return {
            "success": True,
            "data": {
                "comandas": [{
                    "items": [{"order_item_id": str(created_item_id)}],
                }],
                "fired_items_count": 1,
            },
        }

    mock_request = MagicMock()
    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.tables_service._add_tab_items_core",
             new=AsyncMock(return_value={
                 "order_id": order_id,
                 "order_number": 77,
                 "total_amount": 25000.0,
                 "created_order_item_ids": [created_item_id],
                 "promo_savings": 0,
                 "promo_breakdown": [],
                 "promo_lines": [],
             }),
         ), patch(
             "app.services.tables_service.fire_table_items",
             side_effect=capture_fire,
         ):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)

        result = await tables_service.add_tab_items(
            mock_request,
            table_id,
            [{
                "product_id": uuid4(),
                "quantity": 1,
                "unit_price": 25000.0,
                "modifiers": [{
                    "id": str(uuid4()),
                    "name": "Tocineta",
                    "price": 4000.0,
                    "quantity": 1,
                }],
                "notes": "sin cebolla",
            }],
        )

    assert result["success"] is True
    assert result["data"]["fired_items_count"] == 1
    assert captured_item_ids == [[created_item_id]]
    assert old_new_item_id not in captured_item_ids[0]
