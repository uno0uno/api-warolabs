"""Hospitality tax engine — warocol.com#1845."""
from decimal import Decimal

from app.services.hospitality_tax_engine import (
    compute_category_breakdown,
    compute_gl_category_taxes,
    resolve_tax_profile,
    tax_amount_float,
)


def _inc_config(*, included: bool = True, rate: float = 0.08):
    return {
        "inc_applicable": True,
        "inc_rate": rate,
        "inc_included_in_price": included,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
    }


def _iva_config(*, included: bool = False, rate: float = 0.19):
    return {
        "inc_applicable": False,
        "iva_applicable": True,
        "iva_rate": rate,
        "iva_included_in_price": included,
        "liquor_tax_applicable": False,
    }


def _co_full():
    return {
        "inc_applicable": True,
        "inc_rate": 0.08,
        "inc_included_in_price": True,
        "iva_applicable": False,
        "liquor_tax_applicable": True,
        "liquor_tax_rate": 0.05,
    }


def test_co_inc_extractive_matches_legacy_round():
    rows = [{"tax_category": "standard", "subtotal": 10800}]
    std, liq, label = compute_category_breakdown(rows, _inc_config(included=True))
    assert std == 800.0
    assert liq == 0.0
    assert label == "INC 8%"


def test_co_iva_additive():
    rows = [{"tax_category": "standard", "subtotal": 10000}]
    std, liq, label = compute_category_breakdown(rows, _iva_config(included=False))
    assert std == 1900.0
    assert liq == 0.0
    assert label == "IVA 19%"


def test_co_liquor_additive_and_exempt():
    rows = [
        {"tax_category": "standard", "subtotal": 10800},
        {"tax_category": "liquor", "subtotal": 20000},
        {"tax_category": "exempt", "subtotal": 5000},
    ]
    std, liq, _ = compute_category_breakdown(rows, _co_full())
    assert std == 800.0
    assert liq == 1000.0


def test_commercial_tax_lines_standard_and_exempt():
    cfg = {
        "inc_applicable": False,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "commercial_tax_applicable": True,
        "tax_lines": [
            {
                "key": "itbms",
                "label": "ITBMS 7%",
                "rate": 0.07,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "itbms", "liquor": "itbms", "exempt": None},
    }
    rows = [
        {"tax_category": "standard", "subtotal": 10000},
        {"tax_category": "exempt", "subtotal": 3000},
    ]
    std, liq, label = compute_category_breakdown(rows, cfg)
    assert std == 700.0
    assert liq == 0.0
    assert label == "ITBMS 7%"
    profile = resolve_tax_profile(cfg)
    assert profile.line_for_category("exempt") is None
    assert tax_amount_float(10000, profile.primary_line()) == 700.0


def test_commercial_tax_disabled_keeps_lines_but_applies_zero():
    """warocol.com#1868 — flag false → empty profile; tax_lines still on config."""
    cfg = {
        "inc_applicable": False,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "commercial_tax_applicable": False,
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 16%",
                "rate": 0.16,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    }
    rows = [{"tax_category": "standard", "subtotal": 10000}]
    std, liq, label = compute_category_breakdown(rows, cfg)
    assert std == 0.0
    assert liq == 0.0
    profile = resolve_tax_profile(cfg)
    assert profile.lines == {}
    assert profile.primary_line() is None
    # Re-enable restores prior rates without re-seeding.
    cfg["commercial_tax_applicable"] = True
    std_on, _, label_on = compute_category_breakdown(rows, cfg)
    assert std_on == 1600.0
    assert label_on == "IVA 16%"


def test_co_path_ignores_commercial_flag_when_tax_lines_null():
    cfg = _inc_config(included=True)
    cfg["commercial_tax_applicable"] = False
    cfg["tax_lines"] = None
    rows = [{"tax_category": "standard", "subtotal": 10800}]
    std, liq, label = compute_category_breakdown(rows, cfg)
    assert std == 800.0
    assert label == "INC 8%"


def test_gl_decimal_inc_extractive():
    result = compute_gl_category_taxes(
        Decimal("10800"),
        Decimal("0"),
        _inc_config(included=True),
    )
    assert result["standard_is_additive"] is False
    assert result["standard_gl_role"] == "inc"
    # 10800 - 10800/1.08 = 800
    assert result["standard_tax"] == Decimal("800")
