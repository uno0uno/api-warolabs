"""Menu category tax resolve — uno0uno/warocol.com#1883."""
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.tax_config import TaxConfigUpdate
from app.services.hospitality_tax_engine import (
    compute_category_breakdown,
    resolve_effective_tax_category,
    resolve_product_tax_line,
)
from app.services.pos_cart_service import _tax_rows_from_evaluated_lines
from app.services.tenant_config_service import (
    decode_tax_config_jsonb,
    validate_tax_matrix_payload,
)


CAT_A = str(uuid4())
CAT_B = str(uuid4())
CAT_C = str(uuid4())


def _commercial_config(**overrides):
    base = {
        "tax_lines": [
            {
                "key": "mwst",
                "label": "MwSt 19%",
                "rate": 0.19,
                "included_in_price": True,
                "gl_role": "iva",
            },
            {
                "key": "mwst_reduced",
                "label": "MwSt 7%",
                "rate": 0.07,
                "included_in_price": True,
                "gl_role": "iva",
            },
        ],
        "category_map": {"standard": "mwst", "liquor": "mwst_reduced", "exempt": None},
        "commercial_tax_applicable": True,
        "menu_category_line_map": {},
        "exempt_menu_category_ids": [],
    }
    base.update(overrides)
    return base


def test_decode_menu_maps():
    raw = {
        "menu_category_line_map": f'{{"{CAT_A}":"mwst","{CAT_B}":null}}',
        "exempt_menu_category_ids": [CAT_C],
        "tax_lines": "[]",
        "category_map": "{}",
    }
    decoded = decode_tax_config_jsonb(raw)
    assert decoded["menu_category_line_map"][CAT_A] == "mwst"
    assert decoded["menu_category_line_map"][CAT_B] is None
    assert decoded["exempt_menu_category_ids"] == [CAT_C]


def test_validate_menu_map_unknown_line():
    with pytest.raises(HTTPException) as exc:
        validate_tax_matrix_payload(
            [{"key": "mwst", "rate": 0.19}],
            {"standard": "mwst", "exempt": None},
            {CAT_A: "missing"},
            None,
        )
    assert exc.value.status_code == 400
    assert "menu_category_line_map" in str(exc.value.detail)


def test_validate_menu_map_bad_uuid_key():
    with pytest.raises(HTTPException) as exc:
        validate_tax_matrix_payload(
            [{"key": "mwst", "rate": 0.19}],
            None,
            {"not-a-uuid": "mwst"},
            None,
        )
    assert exc.value.status_code == 400


def test_validate_exempt_ids():
    validate_tax_matrix_payload(
        [{"key": "mwst", "rate": 0.19}],
        {"standard": "mwst", "exempt": None},
        {CAT_A: "mwst"},
        [CAT_B],
    )


def test_override_exempt_beats_menu_map():
    cfg = _commercial_config(
        menu_category_line_map={CAT_A: "mwst_reduced"},
    )
    line = resolve_product_tax_line(
        cfg,
        category_id=CAT_A,
        tax_resolution="exempt",
        tax_category="standard",
    )
    assert line is None


def test_override_line_beats_menu_map():
    cfg = _commercial_config(
        menu_category_line_map={CAT_A: "mwst"},
    )
    line = resolve_product_tax_line(
        cfg,
        category_id=CAT_A,
        tax_resolution="line",
        tax_line_key="mwst_reduced",
    )
    assert line is not None
    assert line.key == "mwst_reduced"


def test_exempt_set_before_menu_map():
    cfg = _commercial_config(
        menu_category_line_map={CAT_A: "mwst"},
        exempt_menu_category_ids=[CAT_A],
    )
    line = resolve_product_tax_line(cfg, category_id=CAT_A, tax_resolution="inherit")
    assert line is None


def test_menu_map_then_primary_for_unmapped():
    cfg = _commercial_config(
        menu_category_line_map={CAT_A: "mwst_reduced"},
    )
    mapped = resolve_product_tax_line(cfg, category_id=CAT_A)
    unmapped = resolve_product_tax_line(cfg, category_id=CAT_B)
    assert mapped is not None and mapped.key == "mwst_reduced"
    assert unmapped is not None and unmapped.key == "mwst"  # primary


def test_empty_menu_map_dual_reads_legacy_tax_category():
    cfg = _commercial_config()
    liquor = resolve_product_tax_line(cfg, tax_category="liquor")
    standard = resolve_product_tax_line(cfg, tax_category="standard")
    assert liquor is not None and liquor.key == "mwst_reduced"
    assert standard is not None and standard.key == "mwst"


def test_co_ignores_menu_maps():
    cfg = {
        "inc_applicable": True,
        "inc_rate": 0.08,
        "inc_included_in_price": True,
        "iva_applicable": False,
        "liquor_tax_applicable": True,
        "liquor_tax_rate": 0.05,
        "menu_category_line_map": {CAT_A: "mwst"},
        "exempt_menu_category_ids": [CAT_A],
    }
    # CO path: exempt set must not apply; use tax_category
    line = resolve_product_tax_line(
        cfg,
        category_id=CAT_A,
        tax_resolution="inherit",
        tax_category="standard",
    )
    assert line is not None
    assert line.key == "inc"


def test_effective_category_liquor_bucket():
    cfg = _commercial_config(
        menu_category_line_map={CAT_A: "mwst_reduced"},
    )
    assert (
        resolve_effective_tax_category(cfg, category_id=CAT_A) == "liquor"
    )


def test_tax_config_update_accepts_menu_fields():
    body = TaxConfigUpdate(
        inc_applicable=False,
        inc_included_in_price=False,
        iva_applicable=False,
        iva_included_in_price=False,
        liquor_tax_applicable=False,
        tax_lines=[{"key": "mwst", "rate": 0.19, "gl_role": "iva"}],
        menu_category_line_map={CAT_A: "mwst"},
        exempt_menu_category_ids=[CAT_B],
    )
    assert body.menu_category_line_map[CAT_A] == "mwst"
    assert str(body.exempt_menu_category_ids[0]) == CAT_B


def test_pos_tax_rows_preserve_menu_category_fields():
    """#1889 — do not collapse lines by legacy tax_category."""
    rows = _tax_rows_from_evaluated_lines(
        [
            {
                "tax_category": "standard",
                "tax_resolution": "inherit",
                "tax_line_key": None,
                "category_id": CAT_B,
                "subtotal": 145.0,
            },
            {
                "tax_category": "standard",
                "tax_resolution": "inherit",
                "tax_line_key": None,
                "category_id": CAT_A,
                "net_total": 100.0,
                "subtotal": 100.0,
            },
        ]
    )
    assert len(rows) == 2
    assert rows[0]["category_id"] == CAT_B
    assert rows[0]["tax_resolution"] == "inherit"
    assert rows[1]["subtotal"] == 100.0


def test_breakdown_exempt_vs_mapped_vs_missing_category_id():
    """#1889 regression: missing category_id must not tax exempt categories as primary."""
    cfg = _commercial_config(
        menu_category_line_map={CAT_A: "mwst"},
        exempt_menu_category_ids=[CAT_B],
        category_map={"standard": "mwst", "liquor": "mwst", "exempt": None},
    )
    # Bug shape (old POS): only tax_category
    bug_std, bug_liq, _ = compute_category_breakdown(
        [{"tax_category": "standard", "subtotal": 145.0}],
        cfg,
    )
    assert bug_std + bug_liq > 0

    # Exempt category with inherit
    ok_std, ok_liq, _ = compute_category_breakdown(
        [
            {
                "tax_category": "standard",
                "tax_resolution": "inherit",
                "category_id": CAT_B,
                "subtotal": 145.0,
            }
        ],
        cfg,
    )
    assert ok_std == 0.0
    assert ok_liq == 0.0

    # Mapped category still taxes (bucket uses legacy standard/liquor lines)
    mapped_std, mapped_liq, _ = compute_category_breakdown(
        [
            {
                "tax_category": "standard",
                "tax_resolution": "inherit",
                "category_id": CAT_A,
                "subtotal": 100.0,
            }
        ],
        cfg,
    )
    assert mapped_std + mapped_liq > 0

    # Product override exempt wins over mapped category
    override_std, override_liq, _ = compute_category_breakdown(
        [
            {
                "tax_category": "standard",
                "tax_resolution": "exempt",
                "category_id": CAT_A,
                "subtotal": 100.0,
            }
        ],
        cfg,
    )
    assert override_std == 0.0
    assert override_liq == 0.0


@pytest.mark.asyncio
async def test_get_tenant_tax_config_loads_menu_category_maps():
    """POS tax-preview uses cierre_service loader — must include Facturación maps."""
    from app.services.cierre_service import _get_tenant_tax_config

    captured: dict = {}

    class _Conn:
        async def fetchrow(self, query, *_args):
            captured["query"] = query
            return {
                "inc_applicable": False,
                "inc_rate": None,
                "inc_gl_account_code": None,
                "inc_gl_account_id": None,
                "inc_included_in_price": True,
                "liquor_tax_applicable": False,
                "liquor_tax_rate": None,
                "liquor_tax_gl_account_code": None,
                "liquor_tax_gl_account_id": None,
                "iva_applicable": False,
                "iva_rate": None,
                "iva_gl_account_code": None,
                "iva_gl_account_id": None,
                "iva_included_in_price": False,
                "tax_lines": '[{"key":"iva","label":"IVA 16%","rate":0.16,'
                '"included_in_price":false,"gl_role":"iva"}]',
                "category_map": '{"standard":"iva","liquor":"iva","exempt":null}',
                "commercial_tax_applicable": True,
                "menu_category_line_map": f'{{"{CAT_A}":"iva"}}',
                "exempt_menu_category_ids": [CAT_B],
            }

    cfg = await _get_tenant_tax_config(_Conn(), uuid4())
    assert "menu_category_line_map" in captured["query"]
    assert "exempt_menu_category_ids" in captured["query"]
    assert cfg["menu_category_line_map"][CAT_A] == "iva"
    assert cfg["exempt_menu_category_ids"] == [CAT_B]
    assert resolve_effective_tax_category(
        cfg,
        category_id=CAT_B,
        tax_resolution="inherit",
        tax_category="standard",
    ) == "exempt"
