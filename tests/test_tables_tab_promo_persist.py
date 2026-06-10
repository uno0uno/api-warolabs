"""Persist promo evaluation on mesa tab lines (warocol.com#1020)."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import tables_service
from app.services.promotions_service import (
    apply_promo_eval_to_order_items,
    enrich_order_item_rows_with_promo_basis,
    evaluate_cart_promotions,
    item_rows_to_promo_lines,
    persist_session_tab_promos,
    promo_persist_fields_from_eval_line,
)


def _promo(*, promo_type: str, value_json: dict, name: str = "Tab promo"):
    return {
        "id": uuid4(),
        "name": name,
        "promo_type": promo_type,
        "value_json": value_json,
        "scope_type": "all_products",
        "priority": 10,
        "stackable": False,
        "category_ids": set(),
        "product_ids": set(),
    }


def test_item_rows_to_promo_lines_maps_pending_rows():
    product_id = uuid4()
    item_id = uuid4()
    rows = [{
        "id": item_id,
        "product_id": product_id,
        "category_id": None,
        "quantity": 3,
        "price_at_purchase": 8000.0,
        "promo_eligible_modifier_unit_total": 2000.0,
        "subtotal": 30000.0,
        "tax_category": "standard",
        "promo_opt_out": False,
    }]
    lines = item_rows_to_promo_lines(rows)
    assert lines[0]["id"] == str(item_id)
    assert lines[0]["quantity"] == 3
    assert lines[0]["subtotal"] == 30000.0
    assert lines[0]["promo_eligible_subtotal"] == 30000.0


@pytest.mark.asyncio
async def test_enrich_order_item_rows_with_required_default_modifier_basis():
    item_id = uuid4()
    rows = [{
        "id": item_id,
        "product_id": uuid4(),
        "category_id": None,
        "quantity": 2,
        "price_at_purchase": 10000.0,
        "subtotal": 26000.0,
        "tax_category": "standard",
        "promo_opt_out": False,
    }]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[{
        "order_item_id": item_id,
        "eligible_modifier_unit_total": 2000.0,
    }])

    enriched = await enrich_order_item_rows_with_promo_basis(mock_conn, rows)
    lines = item_rows_to_promo_lines(enriched)

    assert enriched[0]["promo_eligible_modifier_unit_total"] == 2000.0
    assert lines[0]["promo_eligible_subtotal"] == 24000.0


def test_bogo_3x1_on_tab_line_quantity():
    product_id = uuid4()
    lines = [{
        "id": str(uuid4()),
        "product_id": str(product_id),
        "category_id": None,
        "quantity": 3,
        "subtotal": 30000,
    }]
    promos = [_promo(promo_type="bogo", value_json={"buy_qty": 2, "get_qty": 1}, name="3x1")]
    result = evaluate_cart_promotions(lines, promos)
    assert result["promo_savings"] == 10000
    assert result["lines"][0]["promotion_id"] == str(promos[0]["id"])


def test_bogo_custom_buy_get_on_tab_line():
    product_id = uuid4()
    lines = [{
        "id": str(uuid4()),
        "product_id": str(product_id),
        "category_id": None,
        "quantity": 5,
        "subtotal": 50000,
    }]
    promos = [_promo(promo_type="bogo", value_json={"buy_qty": 3, "get_qty": 2}, name="5x3 bundle")]
    result = evaluate_cart_promotions(lines, promos)
    assert result["promo_savings"] == 20000
    assert result["lines"][0]["promotion_name"] == "5x3 bundle"


def test_bogo_wins_over_percent_on_tab_line():
    product_id = uuid4()
    lines = [{
        "id": str(uuid4()),
        "product_id": str(product_id),
        "category_id": None,
        "quantity": 2,
        "subtotal": 20000,
    }]
    bogo = _promo(
        promo_type="bogo",
        value_json={"buy_qty": 1, "get_qty": 1},
        name="BOGO",
    )
    percent = _promo(
        promo_type="percent_off",
        value_json={"percent": 50},
        name="Half off",
    )
    block_map = {"bogo": ["percent_off", "fixed_off"]}
    result = evaluate_cart_promotions(lines, [percent, bogo], promo_type_block_map=block_map)
    assert result["lines"][0]["promotion_name"] == "BOGO"
    assert result["promo_savings"] == 10000


def test_preserve_persisted_promo_when_no_active_promos():
    promo_id = uuid4()
    product_id = uuid4()
    lines = [{
        "id": str(uuid4()),
        "product_id": str(product_id),
        "category_id": None,
        "quantity": 2,
        "subtotal": 20000,
        "applied_promotion_id": str(promo_id),
        "promo_savings_allocated": 10000,
        "promotion_name": "Rebel and Rebel 2x1",
        "promo_type": "bogo",
    }]

    result = evaluate_cart_promotions(
        lines,
        [],
        preserve_persisted_promos=True,
    )

    assert result["promo_savings"] == 10000
    assert result["subtotal_after_promos"] == 10000
    assert result["lines"][0]["promotion_id"] == str(promo_id)
    assert result["lines"][0]["promotion_name"] == "Rebel and Rebel 2x1"
    assert result["promo_breakdown"][0]["promotion_name"] == "Rebel and Rebel 2x1"


def test_line_without_persisted_promo_does_not_get_expired_promo():
    product_id = uuid4()
    lines = [{
        "id": str(uuid4()),
        "product_id": str(product_id),
        "category_id": None,
        "quantity": 2,
        "subtotal": 20000,
    }]

    result = evaluate_cart_promotions(
        lines,
        [],
        preserve_persisted_promos=True,
    )

    assert result["promo_savings"] == 0
    assert result["subtotal_after_promos"] == 20000
    assert "promotion_id" not in result["lines"][0]


def test_promo_opt_out_ignores_persisted_promo_snapshot():
    promo_id = uuid4()
    product_id = uuid4()
    lines = [{
        "id": str(uuid4()),
        "product_id": str(product_id),
        "category_id": None,
        "quantity": 2,
        "subtotal": 20000,
        "promo_opt_out": True,
        "applied_promotion_id": str(promo_id),
        "promo_savings_allocated": 10000,
        "promotion_name": "Rebel and Rebel 2x1",
        "promo_type": "bogo",
    }]

    result = evaluate_cart_promotions(
        lines,
        [],
        preserve_persisted_promos=True,
    )

    assert result["promo_savings"] == 0
    assert result["subtotal_after_promos"] == 20000
    assert "promotion_id" not in result["lines"][0]


@pytest.mark.asyncio
async def test_apply_promo_eval_to_order_items_updates_rows():
    item_id = uuid4()
    promo_id = uuid4()
    item_rows = [{"id": item_id}]
    checkout_eval = {
        "lines": [{
            "id": str(item_id),
            "promotion_id": str(promo_id),
            "promo_savings": 1500,
            "total_discount_allocated": 1500,
            "net_total": 8500,
        }],
    }
    mock_conn = AsyncMock()
    await apply_promo_eval_to_order_items(mock_conn, item_rows, checkout_eval)
    mock_conn.execute.assert_called_once()
    args = mock_conn.execute.call_args.args
    assert args[1] == item_id
    assert args[4] == promo_id
    assert args[5] == 1500


@pytest.mark.asyncio
async def test_persist_session_tab_promos_evaluates_and_recalcs():
    tenant_id = uuid4()
    session_id = uuid4()
    item_id = uuid4()
    product_id = uuid4()
    item_rows = [{
        "id": item_id,
        "product_id": product_id,
        "category_id": None,
        "quantity": 2,
        "subtotal": 20000.0,
        "tax_category": "standard",
        "promo_opt_out": False,
    }]
    checkout_eval = {
        "lines": [{
            "id": str(item_id),
            "promotion_id": str(uuid4()),
            "promo_savings": 10000,
            "total_discount_allocated": 10000,
            "net_total": 10000,
        }],
        "promo_savings": 10000,
        "subtotal_after_promos": 10000,
        "promo_breakdown": [],
    }
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[item_rows, []])

    with patch(
        "app.services.promotions_service.evaluate_checkout_promotions",
        new=AsyncMock(return_value=checkout_eval),
    ) as eval_mock, patch(
        "app.services.promotions_service.apply_promo_eval_to_order_items",
        new=AsyncMock(),
    ) as apply_mock, patch(
        "app.services.promotions_service.recalc_pending_session_order_totals",
        new=AsyncMock(),
    ) as recalc_mock:
        result = await persist_session_tab_promos(mock_conn, tenant_id, session_id)

    eval_mock.assert_awaited_once()
    apply_mock.assert_awaited_once()
    recalc_mock.assert_awaited_once()
    assert result["promo_savings"] == 10000


@pytest.mark.asyncio
async def test_add_tab_items_core_persists_promos_after_insert():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    order_item_id = uuid4()

    session_row = {
        "session_id": session_id,
        "table_name": "Mesa 1",
        "is_bar": False,
        "effective_waiter_member_id": None,
    }
    promo_eval = {
        "lines": [{
            "id": str(order_item_id),
            "promotion_id": str(uuid4()),
            "promo_savings": 5000,
            "promotion_name": "BOGO",
            "promo_type": "bogo",
        }],
        "promo_savings": 5000,
        "promo_breakdown": [{"promotion_name": "BOGO", "savings": 5000}],
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        session_row,
        None,
        {"id": order_id, "order_number": 7, "total_amount": 20000.0},
        {"id": order_item_id},
        {"total_amount": 15000.0},
    ])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()

    items = [{
        "product_id": product_id,
        "quantity": 2,
        "unit_price": 10000.0,
        "modifiers": [],
    }]
    pricing_map = {str(product_id): {"price": 10000, "open_priced": False}}
    persist_mock = AsyncMock(return_value=promo_eval)

    with patch(
        "app.services.tables_service._record_tab_operation_event",
        new=AsyncMock(),
    ), patch(
        "app.services.tables_service._prefetch_product_names",
        new=AsyncMock(return_value={str(product_id): "Pizza"}),
    ), patch(
        "app.services.tables_service.fetch_product_pricing_map",
        new=AsyncMock(return_value=pricing_map),
    ), patch(
        "app.services.tables_service._capture_order_item_ingredients",
        new=AsyncMock(),
    ), patch(
        "app.services.promotions_service.persist_session_tab_promos",
        persist_mock,
    ):
        result = await tables_service._add_tab_items_core(
            mock_conn, tenant_id, user_id, table_id, items,
        )

    persist_mock.assert_awaited_once_with(mock_conn, tenant_id, session_id)
    assert result["promo_savings"] == 5000
    assert result["total_amount"] == 15000.0
    assert result["promo_breakdown"][0]["promotion_name"] == "BOGO"


def test_promo_persist_fields_roundtrip_for_tab_line():
    promo_id = uuid4()
    applied, savings = promo_persist_fields_from_eval_line({
        "promotion_id": str(promo_id),
        "promo_savings": 999.6,
    })
    assert applied == promo_id
    assert savings == 1000
