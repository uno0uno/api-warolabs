"""
Regression guard for issue #144 — mesa-originated sales must be counted in
analytics aggregates. The POS_LIKE_FILTER constants in orders_service and
public_api_service must include all three arms: pos_cart_id, table_session_id,
and extra_attributes->>'source' = 'manual'. If any arm is dropped, mesa or
manual orders silently disappear from /analitica/* metrics.
"""
from app.services.orders_service import (
    POS_LIKE_FILTER as ORDERS_FILTER,
    POS_LIKE_FILTER_ALIAS_O as ORDERS_FILTER_ALIAS,
)
from app.services.public_api_service import (
    POS_LIKE_FILTER as PUBLIC_API_FILTER,
    POS_LIKE_FILTER_ALIAS_O as PUBLIC_API_FILTER_ALIAS,
)


def test_orders_filter_includes_all_three_arms():
    assert "pos_cart_id IS NOT NULL" in ORDERS_FILTER
    assert "table_session_id IS NOT NULL" in ORDERS_FILTER
    assert "extra_attributes->>'source' = 'manual'" in ORDERS_FILTER


def test_orders_filter_alias_includes_all_three_arms():
    assert "o.pos_cart_id IS NOT NULL" in ORDERS_FILTER_ALIAS
    assert "o.table_session_id IS NOT NULL" in ORDERS_FILTER_ALIAS
    assert "o.extra_attributes->>'source' = 'manual'" in ORDERS_FILTER_ALIAS


def test_public_api_filter_includes_all_three_arms():
    assert "pos_cart_id IS NOT NULL" in PUBLIC_API_FILTER
    assert "table_session_id IS NOT NULL" in PUBLIC_API_FILTER
    assert "extra_attributes->>'source' = 'manual'" in PUBLIC_API_FILTER


def test_public_api_filter_alias_includes_all_three_arms():
    assert "o.pos_cart_id IS NOT NULL" in PUBLIC_API_FILTER_ALIAS
    assert "o.table_session_id IS NOT NULL" in PUBLIC_API_FILTER_ALIAS
    assert "o.extra_attributes->>'source' = 'manual'" in PUBLIC_API_FILTER_ALIAS


def test_orders_service_inline_sql_does_not_drop_table_session_id():
    """
    Belt-and-suspenders check: every line in orders_service.py that mentions
    'pos_cart_id IS NOT NULL OR' must also mention 'table_session_id'. Catches
    future inline filters that copy-paste the 2-arm shape.
    """
    import pathlib
    src = pathlib.Path("app/services/orders_service.py").read_text()
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "pos_cart_id IS NOT NULL OR" in line and "table_session_id" not in line:
            raise AssertionError(
                f"orders_service.py:{line_no} has 2-arm POS filter (drops mesa orders): {line.strip()}"
            )


def test_public_api_service_inline_sql_does_not_drop_table_session_id():
    """Same belt-and-suspenders check for public_api_service.py."""
    import pathlib
    src = pathlib.Path("app/services/public_api_service.py").read_text()
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "pos_cart_id IS NOT NULL OR" in line and "table_session_id" not in line:
            raise AssertionError(
                f"public_api_service.py:{line_no} has 2-arm POS filter (drops mesa orders): {line.strip()}"
            )
