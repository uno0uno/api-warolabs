"""Menu category tax resolve — uno0uno/warocol.com#1883."""
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.tax_config import TaxConfigUpdate
from app.services.hospitality_tax_engine import (
    resolve_effective_tax_category,
    resolve_product_tax_line,
)
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
