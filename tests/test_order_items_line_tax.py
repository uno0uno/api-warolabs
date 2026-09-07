"""Order items per-line tax annotation — warocol.com#2044."""
from app.services.orders_service import _attach_order_items_line_tax


def _iva_config(*, included: bool = False, rate: float = 0.19):
    return {
        "inc_applicable": False,
        "iva_applicable": True,
        "iva_rate": rate,
        "iva_included_in_price": included,
        "liquor_tax_applicable": False,
    }


def test_attach_order_items_line_tax_adds_cues():
    items = [
        {
            "id": "a",
            "subtotal": 11900,
            "net_total": 11900,
            "tax_category": "standard",
            "tax_resolution": "inherit",
            "tax_line_key": None,
            "category_id": None,
        },
        {
            "id": "b",
            "subtotal": 5000,
            "net_total": 5000,
            "tax_category": "exempt",
            "tax_resolution": "inherit",
            "tax_line_key": None,
            "category_id": None,
        },
    ]
    cfg = _iva_config(included=True, rate=0.19)
    out = _attach_order_items_line_tax(items, cfg)
    assert out[0]["tax_amount"] > 0
    assert out[0]["tax_label"]
    assert out[0]["included_in_price"] is True
    assert out[1]["tax_amount"] == 0.0
    assert out[1]["tax_label"] is None
