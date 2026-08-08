"""Delete direct purchase: GL void + inventory reverse (#2186, #791)."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.direct_purchase_service import (
    _void_direct_purchase_gl_entry,
    delete_direct_purchase,
)


def _conn_with_tx():
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    return conn


@pytest.mark.asyncio
async def test_void_inventario_gl_creates_reversing_entry():
    tenant_id = uuid4()
    purchase_id = uuid4()
    entry_id = uuid4()
    rev_id = uuid4()
    account_a = uuid4()
    account_b = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "id": entry_id,
                    "entry_date": date(2026, 3, 10),
                    "period_year": 2026,
                    "period_month": 3,
                    "description": "WR-CD-0001",
                    "total_debit": Decimal("100"),
                    "total_credit": Decimal("100"),
                    "source_module": "inventario",
                }
            ],
            [
                {
                    "account_id": account_a,
                    "debit": Decimal("100"),
                    "credit": Decimal("0"),
                    "description": "WR-CD-0001",
                    "line_order": 0,
                },
                {
                    "account_id": account_b,
                    "debit": Decimal("0"),
                    "credit": Decimal("100"),
                    "description": "WR-CD-0001",
                    "line_order": 1,
                },
            ],
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)  # period open
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": rev_id})

    await _void_direct_purchase_gl_entry(conn, tenant_id, purchase_id, "Compra directa eliminada")

    assert conn.execute.await_count >= 3  # void + 2 reverse lines
    void_sql = conn.execute.await_args_list[0].args[0]
    assert "voided" in void_sql
    insert_entry = conn.fetchrow.await_args.args[0]
    assert "'system'" in insert_entry or "system" in insert_entry
    entries_sql = conn.fetch.await_args_list[0].args[0]
    assert "inventario" in entries_sql
    assert "Pago proveedor%" in entries_sql


@pytest.mark.asyncio
async def test_void_supplier_payment_gl_creates_reversing_entry():
    tenant_id = uuid4()
    purchase_id = uuid4()
    entry_id = uuid4()
    rev_id = uuid4()
    account_a = uuid4()
    account_b = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "id": entry_id,
                    "entry_date": date(2026, 3, 11),
                    "period_year": 2026,
                    "period_month": 3,
                    "description": "Pago proveedor WR-CD-0003",
                    "total_debit": Decimal("80"),
                    "total_credit": Decimal("80"),
                    "source_module": "system",
                }
            ],
            [
                {
                    "account_id": account_a,
                    "debit": Decimal("80"),
                    "credit": Decimal("0"),
                    "description": "AP",
                    "line_order": 0,
                },
                {
                    "account_id": account_b,
                    "debit": Decimal("0"),
                    "credit": Decimal("80"),
                    "description": "Cash",
                    "line_order": 1,
                },
            ],
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": rev_id})

    voided = await _void_direct_purchase_gl_entry(
        conn, tenant_id, purchase_id, "Compra directa eliminada"
    )

    assert voided is True
    assert any("voided" in c.args[0] for c in conn.execute.await_args_list if c.args)


@pytest.mark.asyncio
async def test_void_inventario_gl_blocks_closed_period():
    tenant_id = uuid4()
    purchase_id = uuid4()
    entry_id = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": entry_id,
                "entry_date": date(2026, 1, 5),
                "period_year": 2026,
                "period_month": 1,
                "description": "WR-CD-0002",
                "total_debit": Decimal("50"),
                "total_credit": Decimal("50"),
                "source_module": "inventario",
            }
        ]
    )
    conn.fetchval = AsyncMock(return_value=1)  # closed

    with pytest.raises(HTTPException) as exc:
        await _void_direct_purchase_gl_entry(conn, tenant_id, purchase_id)

    assert exc.value.status_code == 400
    assert "período cerrado" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_blocks_closed_purchase_period():
    tenant_id = uuid4()
    purchase_id = uuid4()
    request = MagicMock()
    response = MagicMock()

    conn = _conn_with_tx()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": purchase_id,
            "purchase_number": "WR-CD-0099",
            "purchase_date": datetime(2026, 2, 15),
            "status": "paid",
        }
    )
    conn.fetchval = AsyncMock(return_value=1)  # closed period

    db_cm = MagicMock()
    db_cm.__aenter__ = AsyncMock(return_value=conn)
    db_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "app.services.direct_purchase_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=uuid4()),
    ), patch(
        "app.services.direct_purchase_service.get_db_connection",
        return_value=db_cm,
    ):
        with pytest.raises(HTTPException) as exc:
            await delete_direct_purchase(request, response, purchase_id)

    assert exc.value.status_code == 400
    assert "período cerrado" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_allows_insufficient_stock_going_negative():
    """#791: stock below purchase qty must still reverse (may go negative)."""
    tenant_id = uuid4()
    user_id = uuid4()
    purchase_id = uuid4()
    ingredient_id = uuid4()
    entry_id = uuid4()
    request = MagicMock()
    response = MagicMock()

    conn = _conn_with_tx()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": purchase_id,
                "purchase_number": "WR-CD-0100",
                "purchase_date": datetime(2026, 3, 1),
                "status": "paid",
            },
            {"id": uuid4(), "current_stock": Decimal("2")},  # stock < qty 5 → -3
            {"id": uuid4()},  # reversing JE
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "ingredient_id": ingredient_id,
                    "quantity": Decimal("5"),
                    "unit": "gr",
                }
            ],
            [
                {
                    "id": entry_id,
                    "entry_date": date(2026, 3, 1),
                    "period_year": 2026,
                    "period_month": 3,
                    "description": "WR-CD-0100",
                    "total_debit": Decimal("50"),
                    "total_credit": Decimal("50"),
                    "source_module": "inventario",
                }
            ],
            [
                {
                    "account_id": uuid4(),
                    "debit": Decimal("50"),
                    "credit": Decimal("0"),
                    "description": "line",
                    "line_order": 0,
                },
                {
                    "account_id": uuid4(),
                    "debit": Decimal("0"),
                    "credit": Decimal("50"),
                    "description": "line",
                    "line_order": 1,
                },
            ],
        ]
    )
    conn.execute = AsyncMock(return_value="DELETE 1")

    db_cm = MagicMock()
    db_cm.__aenter__ = AsyncMock(return_value=conn)
    db_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "app.services.direct_purchase_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.direct_purchase_service.get_db_connection",
        return_value=db_cm,
    ):
        result = await delete_direct_purchase(request, response, purchase_id)

    assert result["success"] is True
    update_stock = [
        c.args for c in conn.execute.await_args_list
        if c.args and "UPDATE tenant_inventory" in c.args[0]
    ]
    assert update_stock
    assert update_stock[0][1] == Decimal("-3")  # 2 - 5


@pytest.mark.asyncio
async def test_delete_happy_path_reverses_stock_voids_gl_and_deletes_row():
    tenant_id = uuid4()
    user_id = uuid4()
    purchase_id = uuid4()
    ingredient_id = uuid4()
    entry_id = uuid4()
    request = MagicMock()
    response = MagicMock()

    conn = _conn_with_tx()
    # fetchrow: purchase, inventory, reversing JE id
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": purchase_id,
                "purchase_number": "WR-CD-0101",
                "purchase_date": datetime(2026, 3, 2),
                "status": "paid",
            },
            {"id": uuid4(), "current_stock": Decimal("10")},
            {"id": uuid4()},  # reversing JE
        ]
    )
    # fetchval: purchase period open, then JE period open
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "ingredient_id": ingredient_id,
                    "quantity": Decimal("4"),
                    "unit": "gr",
                }
            ],
            [
                {
                    "id": entry_id,
                    "entry_date": date(2026, 3, 2),
                    "period_year": 2026,
                    "period_month": 3,
                    "description": "WR-CD-0101",
                    "total_debit": Decimal("40"),
                    "total_credit": Decimal("40"),
                    "source_module": "inventario",
                }
            ],
            [
                {
                    "account_id": uuid4(),
                    "debit": Decimal("40"),
                    "credit": Decimal("0"),
                    "description": "line",
                    "line_order": 0,
                },
                {
                    "account_id": uuid4(),
                    "debit": Decimal("0"),
                    "credit": Decimal("40"),
                    "description": "line",
                    "line_order": 1,
                },
            ],
        ]
    )
    conn.execute = AsyncMock(return_value="DELETE 1")

    db_cm = MagicMock()
    db_cm.__aenter__ = AsyncMock(return_value=conn)
    db_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "app.services.direct_purchase_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.direct_purchase_service.get_db_connection",
        return_value=db_cm,
    ):
        result = await delete_direct_purchase(request, response, purchase_id)

    assert result["success"] is True
    assert result["data"]["inventory_reversed"] is True
    sqls = [c.args[0] for c in conn.execute.await_args_list if c.args]
    assert any("tenant_ingredient_movements" in s for s in sqls)
    assert any("voided" in s for s in sqls)
    assert any("DELETE FROM purchase_payments" in s for s in sqls)
    assert any("DELETE FROM tenant_purchases" in s for s in sqls)
