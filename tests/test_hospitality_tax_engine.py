"""Hospitality tax engine — warocol.com#1845."""
from decimal import Decimal

from app.services.hospitality_tax_engine import (
    additive_order_tax_total,
    annotate_line_tax_amounts,
    compute_category_breakdown,
    compute_gl_category_taxes,
    liquor_tax_label_for_config,
    resolve_applicable_tax_lines,
    resolve_effective_tax_category,
    resolve_tax_profile,
    tax_amount_float,
)
from app.services.hospitality_tax_packs import (
    WAVE2_MULTI_TAX_PACKS,
    WAVE2_SIMPLE_TAX_PACKS,
    tax_config_from_wave1_pack,
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


def _mx_config():
    return tax_config_from_wave1_pack(WAVE2_SIMPLE_TAX_PACKS["MX"])


def test_annotate_line_tax_mx_exclusive_sums_to_cart():
    """Per-line IVA 16% amounts reconcile to category-level cart taxes."""
    cfg = _mx_config()
    lines = [
        {
            "id": "a",
            "tax_category": "standard",
            "net_total": 207,
            "tax_resolution": "inherit",
        },
        {
            "id": "b",
            "tax_category": "standard",
            "net_total": 1740,
            "tax_resolution": "inherit",
        },
    ]
    tax_rows = [{"tax_category": "standard", "subtotal": line["net_total"]} for line in lines]
    std, liq, _ = compute_category_breakdown(tax_rows, cfg)
    cart_tax = std + liq
    annotated = annotate_line_tax_amounts(lines, cfg, reconcile_to=(std, liq))
    assert annotated[0]["tax_label"] == "IVA 16%"
    assert annotated[0]["included_in_price"] is False
    assert annotated[0]["tax_amount"] == round(207 * 0.16)
    assert sum(float(l["tax_amount"]) for l in annotated) == cart_tax
    assert cart_tax == round((207 + 1740) * 0.16)


def test_annotate_line_tax_exempt_is_zero():
    cfg = _mx_config()
    lines = [
        {
            "id": "taxable",
            "tax_category": "standard",
            "net_total": 10000,
            "tax_resolution": "inherit",
        },
        {
            "id": "exempt",
            "tax_category": "exempt",
            "net_total": 5000,
            "tax_resolution": "inherit",
        },
        {
            "id": "product_exempt",
            "tax_category": "standard",
            "net_total": 3000,
            "tax_resolution": "exempt",
        },
    ]
    tax_rows = [
        {
            "tax_category": line["tax_category"],
            "tax_resolution": line["tax_resolution"],
            "subtotal": line["net_total"],
        }
        for line in lines
    ]
    std, liq, _ = compute_category_breakdown(tax_rows, cfg)
    annotated = annotate_line_tax_amounts(lines, cfg, reconcile_to=(std, liq))
    assert annotated[0]["tax_amount"] == 1600.0
    assert annotated[1]["tax_amount"] == 0.0
    assert annotated[1]["tax_label"] is None
    assert annotated[2]["tax_amount"] == 0.0
    assert sum(float(l["tax_amount"]) for l in annotated) == std + liq


def test_additive_order_tax_total_mx_vs_co_inc():
    mx = _mx_config()
    assert additive_order_tax_total(371, 0, mx) == 371.0
    assert additive_order_tax_total(0, 371, mx) == 371.0

    co = _inc_config(included=True)
    assert additive_order_tax_total(800, 0, co) == 0.0
    co_liq = _co_full()
    assert additive_order_tax_total(800, 1000, co_liq) == 1000.0


def test_mx_same_key_buckets_to_standard_not_liquor():
    """#1899 — MX maps standard+liquor → iva; tax must land in standard_tax."""
    cfg = _mx_config()
    rows = [{"tax_category": "standard", "subtotal": 580}]
    std, liq, label = compute_category_breakdown(rows, cfg)
    assert label == "IVA 16%"
    assert std == 93.0
    assert liq == 0.0
    assert resolve_effective_tax_category(cfg, tax_category="standard") == "standard"
    assert liquor_tax_label_for_config(cfg) == "IVA 16%"


def test_co_distinct_liquor_still_buckets_liquor():
    cfg = _co_full()
    rows = [
        {"tax_category": "standard", "subtotal": 10800},
        {"tax_category": "liquor", "subtotal": 20000},
    ]
    std, liq, label = compute_category_breakdown(rows, cfg)
    assert std == 800.0
    assert liq == 1000.0
    assert label == "INC 8%"
    assert liquor_tax_label_for_config(cfg) == "IVA licores 5%"
    assert resolve_effective_tax_category(cfg, tax_category="liquor") == "liquor"


def test_co_liquor_honors_included_in_price_column():
    cfg = _co_full()
    cfg["liquor_tax_included_in_price"] = True
    profile = resolve_tax_profile(cfg)
    liquor = profile.line_for_category("liquor")
    assert liquor is not None
    assert liquor.included_in_price is True
    assert liquor.mode == "alternate"
    assert liquor.exclusive_group == "vat"
    # Extractive: 20000 * 0.05 / 1.05 ≈ 952
    assert tax_amount_float(20000, liquor) == 952.0


def test_co_tax_lines_apply_when_commercial_flag_false():
    """#764 — CO may persist tax_lines without enabling commercial flag."""
    cfg = {
        "inc_applicable": False,
        "iva_applicable": True,
        "iva_rate": 0.19,
        "iva_included_in_price": True,
        "liquor_tax_applicable": True,
        "liquor_tax_rate": 0.05,
        "liquor_tax_included_in_price": True,
        "commercial_tax_applicable": False,
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 19%",
                "rate": 0.19,
                "included_in_price": True,
                "gl_role": "iva",
                "mode": "primary",
                "exclusive_group": "vat",
            },
            {
                "key": "liquor",
                "label": "IVA licores 5%",
                "rate": 0.05,
                "included_in_price": True,
                "gl_role": "liquor",
                "mode": "alternate",
                "exclusive_group": "vat",
            },
        ],
        "category_map": {"standard": "iva", "liquor": "liquor", "exempt": None},
    }
    profile = resolve_tax_profile(cfg)
    assert set(profile.lines) == {"iva", "liquor"}
    assert profile.lines["liquor"].included_in_price is True
    assert profile.lines["liquor"].mode == "alternate"


def test_resolve_applicable_alternate_wins_over_primary():
    cfg = tax_config_from_wave1_pack(WAVE2_MULTI_TAX_PACKS["DE"])
    profile = resolve_tax_profile(cfg)
    applied = resolve_applicable_tax_lines(profile, selected_key="mwst_standard")
    assert [line.key for line in applied] == ["mwst_standard"]
    assert applied[0].mode == "alternate"


def test_resolve_applicable_stack_adds_primary_and_selected():
    cfg = {
        "commercial_tax_applicable": True,
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 16%",
                "rate": 0.16,
                "included_in_price": False,
                "gl_role": "iva",
                "mode": "primary",
                "exclusive_group": "vat",
            },
            {
                "key": "tourist",
                "label": "Tourist 2%",
                "rate": 0.02,
                "included_in_price": False,
                "gl_role": "iva",
                "mode": "stack",
            },
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    }
    profile = resolve_tax_profile(cfg)
    applied = resolve_applicable_tax_lines(profile, selected_key="tourist")
    assert [line.key for line in applied] == ["iva", "tourist"]


def test_resolve_applicable_primary_selection():
    cfg = tax_config_from_wave1_pack(WAVE2_MULTI_TAX_PACKS["NL"])
    profile = resolve_tax_profile(cfg)
    applied = resolve_applicable_tax_lines(profile, selected_key="btw_reduced")
    assert [line.key for line in applied] == ["btw_reduced"]
