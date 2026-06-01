"""Checkout stacking: promos → manual → WaRo (B1/B2) → wallet (api-warolabs#371)."""
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.promotions_service import (
    apply_manual_discount_to_evaluated_lines,
    apply_waro_redemption_to_evaluated_lines,
    evaluate_cart_promotions,
)
from app.services.waros_service import (
    _b1_cop_from_waros,
    apply_checkout_waro_redemption,
    compute_redemption_preview,
    preview_redemption,
    settle_waro_redemption,
    evaluate_and_award,
)


def _line(*, subtotal: float, quantity: int = 1, product_id=None, category_id=None, promo_opt_out=False):
    row = {
        "id": str(uuid4()),
        "product_id": str(product_id or uuid4()),
        "category_id": str(category_id) if category_id else None,
        "quantity": quantity,
        "subtotal": subtotal,
    }
    if promo_opt_out:
        row["promo_opt_out"] = True
    return row


def _promo(
    *,
    promo_type: str,
    value_json: dict,
    scope_type: str = "all_products",
    priority: int = 10,
    name: str = "Test promo",
):
    return {
        "id": uuid4(),
        "name": name,
        "promo_type": promo_type,
        "value_json": value_json,
        "scope_type": scope_type,
        "priority": priority,
        "stackable": False,
        "category_ids": set(),
        "product_ids": set(),
    }


def _config_row(**overrides):
    base = {
        "is_enabled": True,
        "redemption_enabled": True,
        "waros_per_1000_cop": 100,
        "max_redeem_percent_per_order": 30.0,
        "min_waros_to_redeem": 1,
        "earn_on_wallet_payment": False,
        "earn_base_excludes_waro_redemption": True,
    }
    base.update(overrides)
    return base


def _checkout_after_promo_manual(
    *,
    subtotal: float = 100_000,
    promo_percent: float = 20,
    manual_amount: float = 8_000,
):
    lines = [_line(subtotal=subtotal)]
    promos = [_promo(promo_type="percent_off", value_json={"percent": promo_percent})]
    evaluated = evaluate_cart_promotions(lines, promos)
    return apply_manual_discount_to_evaluated_lines(evaluated, manual_amount)


def _mock_preview_conn(
    *,
    config: dict,
    wallet_balance: int = 50_000,
    reward_row=None,
):
    conn = AsyncMock()

    async def fetchrow(query, *args):
        q = " ".join(query.split()).lower()
        if "from waro_rewards" in q:
            return reward_row
        if "gamification_config" in q:
            return config
        if "from waros_wallets" in q:
            return {"current_balance": wallet_balance}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


@pytest.mark.asyncio
async def test_promo_manual_b1_preview_matches_checkout_eval():
    """20% promo + manual 8000 + partial B1 — preview total matches layer-4 apply."""
    tenant_id = uuid4()
    customer_id = uuid4()
    checkout = _checkout_after_promo_manual(subtotal=100_000, promo_percent=20, manual_amount=8_000)
    assert checkout["subtotal_after_promos"] == 80_000
    assert checkout["manual_discount_amount"] == 8_000
    assert checkout["total_amount"] == 72_000

    waros_to_redeem = 500
    conn = _mock_preview_conn(config=_config_row(max_redeem_percent_per_order=50.0))

    preview = await compute_redemption_preview(
        conn,
        tenant_id,
        customer_id,
        checkout,
        waros_to_redeem=waros_to_redeem,
    )
    expected_b1 = _b1_cop_from_waros(waros_to_redeem, 100)
    assert preview["b1_cop"] == expected_b1
    assert preview["total_after_redemption"] == preview["checkout_eval"]["total_amount"]

    layered = apply_waro_redemption_to_evaluated_lines(
        dict(checkout),
        preview["total_waro_discount_cop"],
    )
    assert layered["total_amount"] == preview["total_after_redemption"]


@pytest.mark.asyncio
async def test_apply_checkout_waro_redemption_attaches_preview():
    tenant_id = uuid4()
    customer_id = uuid4()
    checkout = _checkout_after_promo_manual()
    conn = _mock_preview_conn(config=_config_row())

    result = await apply_checkout_waro_redemption(
        conn,
        tenant_id,
        customer_id,
        checkout,
        waros_to_redeem=200,
    )
    assert result["total_amount"] < checkout["total_amount"]
    assert "_waro_redemption_preview" in checkout


@pytest.mark.asyncio
async def test_bogo_skipped_on_promo_opt_out_line():
    """B2 free-product lines use promo_opt_out so BOGO does not stack on them."""
    product_id = uuid4()
    bogo = _promo(
        promo_type="bogo",
        value_json={"buy_qty": 1, "get_qty": 1},
        scope_type="products",
        name="BOGO",
    )
    bogo["product_ids"] = {product_id}
    lines = [
        {**_line(subtotal=10_000, quantity=2, product_id=product_id), "promo_opt_out": True},
        _line(subtotal=15_000, quantity=3, product_id=product_id),
    ]
    result = evaluate_cart_promotions(lines, [bogo])
    assert result["lines"][0]["promo_savings"] == 0
    assert result["lines"][0].get("promotion_name") is None
    assert result["lines"][1]["promo_savings"] == 5_000
    assert result["promo_savings"] == 5_000


@pytest.mark.asyncio
async def test_b1_capped_at_max_percent_after_b2_fixed_off():
    """Combined B1+B2: B1 COP capped at max_redeem_percent of base after B2 fixed discount."""
    tenant_id = uuid4()
    customer_id = uuid4()
    checkout = apply_manual_discount_to_evaluated_lines(
        evaluate_cart_promotions([_line(subtotal=100_000)], []),
        0,
    )
    reward_id = uuid4()
    reward_row = {
        "id": reward_id,
        "name": "20k off",
        "reward_type": "fixed_cop_off",
        "waros_cost": 500,
        "fixed_cop_off": 25_000,
        "product_id": None,
        "is_active": True,
    }
    conn = _mock_preview_conn(
        config=_config_row(max_redeem_percent_per_order=30.0),
        wallet_balance=200_000,
        reward_row=reward_row,
    )

    preview = await compute_redemption_preview(
        conn,
        tenant_id,
        customer_id,
        checkout,
        waros_to_redeem=99_999,
        waro_reward_id=reward_id,
    )
    base_canje = 100_000 - 25_000
    max_b1 = int(base_canje * 30 / 100)
    assert preview["b1_cop"] == max_b1
    assert preview["reward_fixed_off"] == 25_000
    assert preview["total_waro_discount_cop"] == max_b1 + 25_000


@pytest.mark.asyncio
async def test_preview_redemption_endpoint_composes_layers():
    """GET preview-redemption runs promo evaluation then redemption preview."""
    tenant_id = uuid4()
    customer_id = uuid4()
    lines = [_line(subtotal=50_000)]
    checkout_stub = {"subtotal_after_promos": 50_000, "manual_discount_amount": 0, "total_amount": 50_000, "lines": lines}
    preview_stub = {
        "total_after_redemption": 45_000,
        "checkout_eval": {**checkout_stub, "total_amount": 45_000},
    }
    session = MagicMock(tenant_id=tenant_id)
    request = MagicMock()

    @asynccontextmanager
    async def fake_conn():
        yield AsyncMock()

    with patch("app.services.waros_service.require_valid_session", return_value=session), patch(
        "app.services.waros_service.get_db_connection",
        side_effect=lambda **kwargs: fake_conn(),
    ), patch(
        "app.services.promotions_service.evaluate_checkout_promotions",
        new_callable=AsyncMock,
        return_value=checkout_stub,
    ) as mock_promo, patch(
        "app.services.waros_service.compute_redemption_preview",
        new_callable=AsyncMock,
        return_value=preview_stub,
    ) as mock_waro:
        result = await preview_redemption(
            request,
            lines,
            customer_id=customer_id,
            manual_discount_amount=0,
            waros_to_redeem=100,
        )

    mock_promo.assert_awaited_once()
    mock_waro.assert_awaited_once()
    assert result["total_after_redemption"] == 45_000
    assert "checkout_eval" not in result


def test_customer_wallet_service_never_calls_evaluate_and_award():
    """Wallet recharge path must not award WaRos (epic stacking contract)."""
    src = Path(__file__).resolve().parents[1] / "app/services/customer_wallet_service.py"
    assert "evaluate_and_award" not in src.read_text()


@pytest.mark.asyncio
async def test_evaluate_and_award_skips_when_wallet_payment_and_earn_disabled():
    tenant_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    conn = AsyncMock()

    async def fetchrow(query, *args):
        q = " ".join(query.split()).lower()
        if "gamification_config" in q:
            return {
                "is_enabled": True,
                "max_daily_waros": 0,
                "earn_on_wallet_payment": False,
                "earn_base_excludes_waro_redemption": True,
            }
        if "from orders" in q and "total_amount" in q:
            return {
                "total_amount": 40_000.0,
                "waro_redeemed_amount_cop": 0.0,
                "payment_method": "customer_wallet",
            }
        return {"total": 1, "total_qty": 1, "today_earned": 0, "freq_count": 0}

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def fake_db(**kwargs):
        yield conn

    with patch("app.services.waros_service.get_db_connection", side_effect=fake_db):
        awarded = await evaluate_and_award(order_id, customer_id, tenant_id)

    assert awarded == 0
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_and_award_reduces_base_for_wallet_split_payment():
    tenant_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    conn = AsyncMock()
    fetch_calls = []

    async def fetchrow(query, *args):
        q = " ".join(query.split()).lower()
        fetch_calls.append(q)
        if "gamification_config" in q:
            return {
                "is_enabled": True,
                "max_daily_waros": 0,
                "earn_on_wallet_payment": False,
                "earn_base_excludes_waro_redemption": False,
            }
        if "from orders" in q and "total_amount" in q:
            return {
                "total_amount": 50_000.0,
                "waro_redeemed_amount_cop": 0.0,
                "payment_method": "cash",
            }
        if "count(*)" in q and "completed" in q and "frequency" not in q:
            return {"total": 0}
        if "sum(quantity)" in q:
            return {"total_qty": 0}
        if "today_earned" in q or "transaction_type = 'earned'" in q:
            return {"today_earned": 0}
        return None

    async def fetchval(query, *args):
        if "customer_wallet" in query:
            return 15_000
        return 0

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(
        return_value=[{"rule_type": "ticket_value", "config": {"base_waros": 1, "base_pesos": 1000}}],
    )
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

    write_conn = AsyncMock()
    write_conn.execute = AsyncMock()
    write_conn.fetchrow = AsyncMock(return_value={"current_balance": 35})
    write_conn.transaction = conn.transaction

    ctx_count = {"n": 0}

    @asynccontextmanager
    async def fake_db(**kwargs):
        ctx_count["n"] += 1
        if kwargs.get("use_transaction") is False:
            yield conn
        else:
            yield write_conn

    with patch("app.services.waros_service.get_db_connection", side_effect=fake_db):
        awarded = await evaluate_and_award(order_id, customer_id, tenant_id)

    assert awarded == 35
    conn.fetchval.assert_awaited()


@pytest.mark.asyncio
async def test_evaluate_and_award_restores_waro_redemption_to_earn_base():
    tenant_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    conn = AsyncMock()

    async def fetchrow(query, *args):
        q = " ".join(query.split()).lower()
        if "gamification_config" in q:
            return {
                "is_enabled": True,
                "max_daily_waros": 0,
                "earn_on_wallet_payment": True,
                "earn_base_excludes_waro_redemption": True,
            }
        if "from orders" in q and "total_amount" in q:
            return {
                "total_amount": 42_000.0,
                "waro_redeemed_amount_cop": 8_000.0,
                "payment_method": "cash",
            }
        if "count(*)" in q and "completed" in q:
            return {"total": 0}
        if "sum(quantity)" in q:
            return {"total_qty": 0}
        if "today_earned" in q:
            return {"today_earned": 0}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(
        return_value=[{"rule_type": "ticket_value", "config": {"base_waros": 1, "base_pesos": 1000}}],
    )

    write_conn = AsyncMock()
    write_conn.execute = AsyncMock()
    write_conn.fetchrow = AsyncMock(return_value={"current_balance": 50})
    write_conn.transaction = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

    @asynccontextmanager
    async def fake_db(**kwargs):
        if kwargs.get("use_transaction") is False:
            yield conn
        else:
            yield write_conn

    with patch("app.services.waros_service.get_db_connection", side_effect=fake_db):
        awarded = await evaluate_and_award(order_id, customer_id, tenant_id)

    assert awarded == 50


@pytest.mark.asyncio
async def test_settle_waro_redemption_debits_wallet_and_writes_rows():
    tenant_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"current_balance": 1_000})
    conn.execute = AsyncMock()

    preview = {
        "total_waros_cost": 300,
        "total_waro_discount_cop": 3_000,
        "b1_waros": 300,
        "b1_cop": 3_000,
        "b2_waros": 0,
        "reward_type": None,
        "waro_reward_id": None,
    }
    await settle_waro_redemption(conn, tenant_id, customer_id, order_id, preview)

    queries = [" ".join(c.args[0].split()).lower() for c in conn.execute.await_args_list]
    assert any("update waros_wallets" in q for q in queries)
    assert any("insert into waros_transactions" in q for q in queries)
    assert any("waros_redeemed" in q for q in queries)
    for call in conn.fetchrow.await_args_list:
        assert "for update" in " ".join(call.args[0].split()).lower()


@pytest.mark.asyncio
async def test_settle_free_product_inserts_line_with_promo_opt_out():
    tenant_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"current_balance": 500},
            {"id": uuid4()},
        ],
    )
    conn.execute = AsyncMock()

    preview = {
        "total_waros_cost": 100,
        "total_waro_discount_cop": 0,
        "b1_waros": 0,
        "b1_cop": 0,
        "b2_waros": 100,
        "reward_type": "free_product",
        "waro_reward_id": str(uuid4()),
        "free_product_id": str(product_id),
    }
    await settle_waro_redemption(conn, tenant_id, customer_id, order_id, preview)

    item_inserts = [
        c
        for c in conn.fetchrow.await_args_list
        if "order_items" in c.args[0].lower()
    ]
    assert item_inserts
    assert item_inserts[0].args[0].lower().count("promo_opt_out") >= 1


@pytest.mark.asyncio
async def test_settle_propagates_db_failure_for_transaction_rollback():
    tenant_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"current_balance": 1_000})
    conn.execute = AsyncMock(side_effect=RuntimeError("payment failed"))

    preview = {
        "total_waros_cost": 50,
        "total_waro_discount_cop": 500,
        "b1_waros": 50,
        "b1_cop": 500,
        "b2_waros": 0,
    }
    with pytest.raises(RuntimeError, match="payment failed"):
        await settle_waro_redemption(conn, tenant_id, customer_id, order_id, preview)


@pytest.mark.asyncio
async def test_settle_rejects_insufficient_waro_balance():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"current_balance": 10})
    preview = {"total_waros_cost": 100, "total_waro_discount_cop": 1_000, "b1_waros": 100, "b1_cop": 1_000, "b2_waros": 0}
    with pytest.raises(HTTPException) as exc:
        await settle_waro_redemption(conn, uuid4(), uuid4(), uuid4(), preview)
    assert exc.value.status_code == 422
