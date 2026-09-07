"""Tip tax helpers — warocol.com#740."""
from app.services.tip_tax_service import (
    compute_tip_tax_amount,
    split_settlement_amount_due,
    tip_settlement_total,
)


def _iva_config(*, included: bool = False, rate: float = 0.19):
    return {
        "inc_applicable": False,
        "iva_applicable": True,
        "iva_rate": rate,
        "iva_included_in_price": included,
    }


def test_compute_tip_tax_zero_when_not_taxable():
    assert compute_tip_tax_amount(10_000, False, _iva_config()) == 0.0


def test_compute_tip_tax_iva_excluded():
    assert compute_tip_tax_amount(10_000, True, _iva_config(included=False)) == 1900.0


def test_compute_tip_tax_iva_included():
    assert compute_tip_tax_amount(11_900, True, _iva_config(included=True)) == 1900.0


def test_split_settlement_includes_tip_tax():
    assert split_settlement_amount_due(100_000, 10_000, 1_900) == 111_900


def test_tip_settlement_total():
    assert tip_settlement_total(5_000, 950) == 5_950


def test_compute_tip_tax_from_tax_lines():
    cfg = {
        "inc_applicable": False,
        "iva_applicable": False,
        "tax_lines": [
            {
                "key": "gst",
                "label": "GST 10%",
                "rate": 0.10,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "gst", "exempt": None},
    }
    assert compute_tip_tax_amount(10_000, True, cfg) == 1000.0
