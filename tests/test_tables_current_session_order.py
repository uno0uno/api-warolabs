from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import tables_service


class _DbContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_current_session_tab_items_are_ordered_chronologically():
    tenant_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": table_id, "name": "Mesa 1", "status": "occupied"},
        {
            "id": session_id,
            "opened_at": now,
            "opened_by_user_id": uuid4(),
            "attended_by_member_id": None,
            "attended_by_member_name": None,
            "attended_by_member_role": None,
            "effective_waiter_member_id": None,
            "effective_waiter_member_name": None,
            "effective_waiter_member_role": None,
            "duration_minutes": 8.5,
            "running_total": 0,
            "order_count": 0,
            "minimum_consumption_enabled_snapshot": False,
            "minimum_consumption_amount_snapshot": 0,
            "minimum_consumption_restrictive_snapshot": False,
        },
    ])

    conn.fetch = AsyncMock(side_effect=[
        [],
        [{
            "order_item_id": uuid4(),
            "product_id": uuid4(),
            "category_id": None,
            "product_name": "Pizza",
            "quantity": 1,
            "price_at_purchase": 12000,
            "subtotal": 12000,
            "notes": "Sin cebolla",
            "promo_opt_out": False,
            "applied_promotion_id": None,
            "promo_savings_allocated": 0,
            "promotion_name": None,
            "promo_type": None,
            "fulfillment_status": "new",
            "sent_at": None,
            "modifiers": [],
        }],
        [],
    ])

    with patch(
        "app.services.tables_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.tables_service.get_db_connection",
        return_value=_DbContext(conn),
    ), patch(
        "app.services.tables_service._get_tenant_tax_config",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.services.table_session_advances_service.get_session_advances_payload",
        new=AsyncMock(return_value={
            "advances": [],
            "advance_totals": {"active_total_cop": 0},
        }),
    ):
        result = await tables_service.get_current_session(object(), table_id)

    assert result["success"] is True
    assert result["data"]["tab_items"][0]["productName"] == "Pizza"

    tab_items_query = conn.fetch.await_args_list[1].args[0]
    assert "ORDER BY oi.created_at ASC, oi.id ASC" in tab_items_query
    assert "ORDER BY oim.created_at" in tab_items_query
