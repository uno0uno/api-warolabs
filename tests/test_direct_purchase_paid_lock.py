"""Paid direct purchases are immutable (#2513)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.direct_purchase_service import delete_direct_purchase, update_direct_purchase


def _conn_with_tx():
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    return conn


@pytest.mark.asyncio
async def test_update_blocks_paid_direct_purchase():
    tenant_id = uuid4()
    purchase_id = uuid4()
    request = MagicMock()
    response = MagicMock()

    conn = _conn_with_tx()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": purchase_id,
            "status": "paid",
            "purchase_number": "WR-CD-0023",
            "tenant_id": tenant_id,
            "payment_type": "contado",
        }
    )

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
            await update_direct_purchase(
                request,
                response,
                purchase_id,
                items_data='[{"ingredient_id": "00000000-0000-0000-0000-000000000001", "quantity": 1, "unit_cost": 10}]',
            )

    assert exc.value.status_code == 409
    assert "editar" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_blocks_paid_direct_purchase():
    tenant_id = uuid4()
    purchase_id = uuid4()
    request = MagicMock()
    response = MagicMock()

    conn = _conn_with_tx()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": purchase_id,
            "purchase_number": "WR-CD-0023",
            "purchase_date": datetime(2026, 7, 29),
            "status": "paid",
        }
    )

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

    assert exc.value.status_code == 409
    assert "eliminar" in exc.value.detail
