from app.services.orders_service import _append_orders_source_payment_filters


def test_source_pos_and_paid():
    where: list[str] = []
    params: list = []
    count = _append_orders_source_payment_filters(
        where, params, 1, source="pos", payment_status="paid"
    )
    assert "o.table_session_id IS NULL AND o.delivery_address_id IS NULL" in where
    assert where[-1] == "o.payment_status = $2"
    assert params == ["paid"]
    assert count == 2


def test_source_mesa_barra_delivery():
    where = []
    _append_orders_source_payment_filters(where, [], 1, source="mesa")
    assert "t_meta.is_bar" in where[0] and "NOT TRUE" in where[0]

    where = []
    _append_orders_source_payment_filters(where, [], 1, source="barra")
    assert "IS TRUE" in where[0]

    where = []
    _append_orders_source_payment_filters(where, [], 1, source="delivery")
    assert where == ["o.delivery_address_id IS NOT NULL"]


def test_delivery_only_alias_and_unpaid():
    where = []
    _append_orders_source_payment_filters(where, [], 1, delivery_only=True, payment_status="unpaid")
    assert "o.delivery_address_id IS NOT NULL" in where
    assert any("payment_status IS NULL" in clause for clause in where)
