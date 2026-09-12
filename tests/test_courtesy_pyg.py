"""P&G courtesy breakout (uno0uno/warocol.com#2675)."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_pl_includes_courtesy_cost_without_touching_totals():
    from app.services import accounting_service

    conn = MagicMock()

    async def _fetchrow(query, *args):
        if "orden_cortesia" in query:
            return {"total": Decimal("45000")}
        if "closing_summary" in query:
            return {"total_sales": Decimal("1000000")}
        if "tenant_expenses" in query:
            return {"total": Decimal("300000")}
        if "tenant_purchases" in query:
            return {"total": Decimal("0")}
        return {"total": Decimal("0")}

    conn.fetchrow = _fetchrow
    conn.fetch = AsyncMock(return_value=[])

    result = await accounting_service._compute_pl_for_period(
        conn, uuid4(), 2026, 9, False
    )

    assert result.cogs.courtesy_cost == 45000.0
    assert result.cogs.food_cost == 300000.0
    assert result.cogs.total == 300000.0
    assert result.gross_profit == 700000.0
