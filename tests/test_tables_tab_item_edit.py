"""Tab line content edit gate (#1151)."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import tables_service


def test_tab_item_edit_block_reason_preparing():
    reason = tables_service._tab_item_edit_block_reason("preparing", None)
    assert reason is not None
    assert "Preparando" in reason


def test_tab_item_edit_block_reason_sent_pending_comanda():
    assert tables_service._tab_item_edit_block_reason("sent", "pending") is None


def test_tab_item_edit_block_reason_new():
    assert tables_service._tab_item_edit_block_reason("new", None) is None


@pytest.mark.asyncio
async def test_get_tab_item_edit_eligibility_blocked_records_attempt():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    order_item_id = uuid4()
    order_id = uuid4()

    row = {
        "id": order_item_id,
        "product_id": uuid4(),
        "quantity": 1,
        "price_at_purchase": 10.0,
        "subtotal": 10.0,
        "notes": None,
        "fulfillment_status": "preparing",
        "order_id": order_id,
        "total_amount": 10.0,
        "order_number": 1,
        "product_name": "Tomate",
        "table_name": "Mesa 1",
        "is_bar": False,
        "table_session_id": uuid4(),
        "effective_waiter_member_id": None,
    }
    comanda_ctx = {
        "comanda_item_id": uuid4(),
        "comanda_item_status": "pending",
        "comanda_id": uuid4(),
        "comanda_status": "preparing",
    }

    mock_conn = AsyncMock()
    record_mock = AsyncMock()

    request = AsyncMock()

    with patch(
        "app.services.tables_service.require_valid_session",
        return_value=AsyncMock(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.tables_service.get_db_connection",
    ) as conn_ctx, patch(
        "app.services.tables_service._fetch_open_tab_item_row",
        new=AsyncMock(return_value=row),
    ), patch(
        "app.services.tables_service._fetch_tab_item_comanda_context",
        new=AsyncMock(return_value=comanda_ctx),
    ), patch(
        "app.services.tables_service._record_tab_operation_event",
        record_mock,
    ):
        conn_ctx.return_value.__aenter__.return_value = mock_conn
        with pytest.raises(APIError) as exc:
            await tables_service.get_tab_item_edit_eligibility(
                request, table_id, order_item_id, record_attempt=True,
            )
        assert exc.value.status_code == 409
        assert exc.value.details.get("code") == "TAB_ITEM_EDIT_KITCHEN_ACCEPTED"
        record_mock.assert_awaited_once()
        assert record_mock.call_args.kwargs["action"] == "tab_item_edit_blocked"
