"""WaRo redemption summary on order responses (api-warolabs#375)."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.orders_service import _get_order_waro_redemption_summary


@pytest.mark.asyncio
async def test_get_order_waro_redemption_summary_aggregates_b2_row():
    order_id = uuid4()
    reward_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "redemption_type": "reward_fixed_cop",
                "waros_spent": 200,
                "cop_discount": 5000.0,
                "waro_reward_id": reward_id,
                "reward_name": "Empanada gratis",
            },
        ]
    )

    result = await _get_order_waro_redemption_summary(conn, order_id)

    assert result["waro_discount_cop"] == 5000.0
    assert result["waros_spent"] == 200
    assert len(result["waro_breakdown"]) == 1
    entry = result["waro_breakdown"][0]
    assert entry["redemption_type"] == "reward_fixed_cop"
    assert entry["reward_name"] == "Empanada gratis"
    assert entry["waro_reward_id"] == str(reward_id)
    assert conn.fetch.await_args.args[1] == order_id


@pytest.mark.asyncio
async def test_get_order_waro_redemption_summary_empty_order():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    result = await _get_order_waro_redemption_summary(conn, uuid4())

    assert result == {
        "waro_discount_cop": 0.0,
        "waros_spent": 0,
        "waro_breakdown": [],
    }
