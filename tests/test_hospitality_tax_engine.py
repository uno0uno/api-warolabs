"""Hospitality tax engine — warocol.com#1845."""
from decimal import Decimal

from app.services.hospitality_tax_engine import (
    additive_order_tax_total,
    annotate_line_tax_amounts,
    compute_category_breakdown,
    compute_gl_category_taxes,
    compute_items_tax_totals,
    liquor_tax_label_for_config,
    resolve_applicable_tax_lines,
    resolve_effective_tax_category,
    resolve_product_tax_lines,
    resolve_tax_profile,
    sync_co_tax_lines_for_sales_profile,
    tax_amount_decimal,
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


def test_mx_pack_commercial_off_returns_zero_tax():
    """api-warolabs#773 — MX no-tax = flag false; pack lines may remain stored."""
    cfg = _mx_config()
    assert cfg["commercial_tax_applicable"] is True
    rows = [{"tax_category": "standard", "subtotal": 10000}]
    std_on, liq_on, label_on = compute_category_breakdown(rows, cfg)
    assert std_on == 1600.0
    assert liq_on == 0.0
    assert label_on == "IVA 16%"

    cfg["commercial_tax_applicable"] = False
    std_off, liq_off, label_off = compute_category_breakdown(rows, cfg)
    assert std_off == 0.0
    assert liq_off == 0.0
    assert label_off == "Impuesto"
    assert resolve_tax_profile(cfg).primary_line() is None
    assert cfg["tax_lines"][0]["label"] == "IVA 16%"

    gl = compute_items_tax_totals(rows, cfg)
    assert gl["standard_tax"] == Decimal("0")
    assert gl["liquor_tax"] == Decimal("0")


def test_mx_pack_none_flag_still_applies_tax_lines():
    """#773 — None is not disable; only explicit False empties commercial profile."""
    cfg = _mx_config()
    cfg["commercial_tax_applicable"] = None
    std, _, label = compute_category_breakdown(
        [{"tax_category": "standard", "subtotal": 10000}],
        cfg,
    )
    assert std == 1600.0
    assert label == "IVA 16%"


def test_co_exempt_columns_unchanged_with_commercial_false():
    """#773 regression — CO no IVA/INC stays zero; INC path still works."""
    exempt = {
        "inc_applicable": False,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "commercial_tax_applicable": False,
        "tax_lines": None,
    }
    std, liq, _ = compute_category_breakdown(
        [{"tax_category": "standard", "subtotal": 10000}],
        exempt,
    )
    assert std == 0.0
    assert liq == 0.0

    taxed = _iva_config(included=False, rate=0.19)
    taxed["commercial_tax_applicable"] = False
    taxed["tax_lines"] = None
    std_iva, _, label = compute_category_breakdown(
        [{"tax_category": "standard", "subtotal": 10000}],
        taxed,
    )
    assert std_iva == 1900.0
    assert label == "IVA 19%"


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
    # Per-item rounding (#765) — not one round on the summed base.
    assert cart_tax == round(207 * 0.16) + round(1740 * 0.16)


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


def test_co_liquor_included_gl_extract_not_additive():
    """#765 — Bebidas→liquor Incluido: extract 30000@5% ≈ 1428.57, not 1500."""
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
        "menu_category_line_map": {
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb": "liquor",
        },
    }
    cat = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    applied = resolve_product_tax_lines(cfg, category_id=cat)
    assert [line.key for line in applied] == ["liquor"]
    tax, is_additive = tax_amount_decimal(Decimal("30000"), applied[0])
    assert is_additive is False
    assert tax == Decimal("30000") - (Decimal("30000") / Decimal("1.05"))
    assert abs(float(tax) - 1428.57142857) < 1e-6

    totals = compute_items_tax_totals(
        [{"category_id": cat, "subtotal": Decimal("30000")}],
        cfg,
    )
    assert totals["liquor_is_additive"] is False
    assert totals["liquor_additive"] == Decimal("0")
    assert totals["liquor_tax"] == tax
    assert additive_order_tax_total(0, float(tax), cfg) == 0.0

    # Fallback subtotal path also honors included flag.
    gl = compute_gl_category_taxes(Decimal("0"), Decimal("30000"), cfg)
    assert gl["liquor_is_additive"] is False
    assert gl["liquor_tax"] == tax


def test_override_mapped_category_only_alternate_rate():
    """#765 — override: mapped Bebidas use liquor only, not IVA+liquor."""
    cfg = tax_config_from_wave1_pack(WAVE2_MULTI_TAX_PACKS["DE"])
    cat = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    cfg["menu_category_line_map"] = {cat: "mwst_standard"}
    applied = resolve_product_tax_lines(cfg, category_id=cat)
    assert [line.key for line in applied] == ["mwst_standard"]
    rows = [{"category_id": cat, "subtotal": 10000}]
    std, liq, _ = compute_category_breakdown(rows, cfg)
    assert std == 0.0
    assert liq == 1900.0
    assert resolve_effective_tax_category(cfg, category_id=cat) == "liquor"


def test_stack_mode_sums_primary_and_selected():
    """#765 — stack adds both primary and selected tributes."""
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
        "menu_category_line_map": {
            "dddddddd-dddd-dddd-dddd-dddddddddddd": "tourist",
        },
    }
    cat = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    applied = resolve_product_tax_lines(cfg, category_id=cat)
    assert [line.key for line in applied] == ["iva", "tourist"]
    rows = [{"category_id": cat, "subtotal": 10000}]
    std, liq, _ = compute_category_breakdown(rows, cfg)
    assert std == 1600.0 + 200.0
    assert liq == 0.0

    annotated = annotate_line_tax_amounts(
        [{
            "id": "1",
            "category_id": cat,
            "net_total": 10000,
            "tax_resolution": "inherit",
        }],
        cfg,
        reconcile_to=(std, liq),
    )
    assert annotated[0]["tax_amount"] == 1800.0
    assert "IVA 16%" in annotated[0]["tax_label"]
    assert "Tourist 2%" in annotated[0]["tax_label"]

    totals = compute_items_tax_totals(rows, cfg)
    assert totals["standard_tax"] == Decimal("1800")
    assert totals["standard_is_additive"] is True
    assert totals["standard_additive"] == Decimal("1800")


def test_menu_mapped_liquor_not_lost_when_legacy_tax_category_standard():
    """#2035 — mesa close must not GROUP BY tax_category only (INC eats liquor)."""
    liquor_cat = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    cfg = {
        "inc_applicable": True,
        "inc_rate": 0.08,
        "inc_included_in_price": True,
        "iva_applicable": False,
        "liquor_tax_applicable": True,
        "liquor_tax_rate": 0.05,
        "liquor_tax_included_in_price": True,
        "tax_lines": [
            {
                "key": "inc",
                "label": "INC 8%",
                "rate": 0.08,
                "included_in_price": True,
                "gl_role": "inc",
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
        "category_map": {"standard": "inc", "liquor": "liquor", "exempt": None},
        "menu_category_line_map": {liquor_cat: "liquor"},
    }
    # Legacy product tag is still standard — liquor comes from menu map.
    item_rows = [
        {"tax_category": "standard", "category_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "subtotal": 238000},
        {"tax_category": "standard", "category_id": liquor_cat, "subtotal": 30000},
    ]
    std, liq, label = compute_category_breakdown(item_rows, cfg)
    assert label == "INC 8%"
    assert std == 17630.0  # 238000 * 0.08/1.08
    assert liq == 1429.0   # 30000 * 0.05/1.05

    # Buggy mesa-close shape: collapsed by tax_category only → liquor vanishes.
    collapsed = [{"tax_category": "standard", "subtotal": 268000}]
    buggy_std, buggy_liq, _ = compute_category_breakdown(collapsed, cfg)
    assert buggy_liq == 0.0
    assert buggy_std == 19852.0


def test_sync_co_tax_lines_flips_iva_to_inc_and_keeps_custom():
    """#2031 — Perfil de ventas must rewrite tax_lines so POS is not hybrid."""
    cat = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    cfg = {
        "iva_applicable": True,
        "iva_rate": 0.19,
        "iva_included_in_price": True,
        "inc_applicable": False,
        "inc_rate": 0.08,
        "inc_included_in_price": True,
        "liquor_tax_applicable": True,
        "liquor_tax_rate": 0.05,
        "liquor_tax_included_in_price": True,
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
            {
                "key": "bebidas",
                "label": "Bebidas 5%",
                "rate": 0.05,
                "included_in_price": True,
                "gl_role": "iva",
                "mode": "alternate",
                "exclusive_group": "vat",
            },
        ],
        "category_map": {"standard": "iva", "liquor": "liquor", "exempt": None},
        "menu_category_line_map": {cat: "iva"},
    }
    lines, category_map, menu_map = sync_co_tax_lines_for_sales_profile(
        cfg,
        iva_applicable=False,
        inc_applicable=True,
    )
    assert [x["key"] for x in lines] == ["inc", "liquor", "bebidas"]
    assert lines[0]["label"] == "INC 8%"
    assert lines[0]["mode"] == "primary"
    assert category_map["standard"] == "inc"
    assert category_map["liquor"] == "liquor"
    assert menu_map[cat] == "inc"


def test_sync_co_tax_lines_clears_primary_when_non_responsible():
    lines, category_map, menu_map = sync_co_tax_lines_for_sales_profile(
        {
            "tax_lines": [
                {
                    "key": "iva",
                    "label": "IVA 19%",
                    "rate": 0.19,
                    "mode": "primary",
                    "gl_role": "iva",
                },
            ],
            "menu_category_line_map": {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": "iva"},
        },
        iva_applicable=False,
        inc_applicable=False,
    )
    assert lines == []
    assert category_map["standard"] is None
    assert menu_map["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"] is None
