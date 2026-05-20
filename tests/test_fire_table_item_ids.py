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
