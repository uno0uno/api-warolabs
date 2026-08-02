"""Profile-driven hospitality tax lines for POS, orders, cierre, and tips.

Resolves tenant_tax_config into tax_lines[] + category→line map. Colombia's
INC/IVA/liquor columns adapt into the same shape so numeric results stay stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import json


TAX_LINE_MODES = frozenset({"primary", "alternate", "stack"})


@dataclass(frozen=True)
class TaxLine:
    key: str
    label: str
    rate: float
    included_in_price: bool
    gl_role: str  # resolve_tax_account kind: inc | iva | liquor
    gl_account_id: Any = None
    gl_account_code: Optional[str] = None
    mode: str = "primary"  # primary | alternate | stack
    exclusive_group: Optional[str] = None  # e.g. vat — refuse stack within same group


@dataclass(frozen=True)
class TaxProfile:
    lines: Dict[str, TaxLine]
    category_map: Dict[str, Optional[str]]

    def line_for_category(self, category: str) -> Optional[TaxLine]:
        key = self.category_map.get(category or "standard")
        if not key:
            return None
        return self.lines.get(key)

    def primary_line(self) -> Optional[TaxLine]:
        for line in self.lines.values():
            if line.mode == "primary":
                return line
        return self.line_for_category("standard")


def _as_mapping_list(raw: Any) -> List[Mapping[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _as_category_map(raw: Any) -> Dict[str, Optional[str]]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, Optional[str]] = {}
    for key, value in raw.items():
        out[str(key)] = None if value in (None, "", "null") else str(value)
    return out


def _as_menu_category_line_map(raw: Any) -> Dict[str, Optional[str]]:
    """Menu category UUID string → tax line key (same shape as category_map)."""
    return _as_category_map(raw)


def _as_uuid_str_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {raw.strip()} if raw.strip() else set()
    if not isinstance(raw, (list, tuple, set)):
        return set()
    out: set[str] = set()
    for item in raw:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.add(s)
    return out


def _row_field(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _resolve_selected_tax_line_key(
    tax_config: Mapping[str, Any],
    profile: TaxProfile,
    *,
    category_id: Any = None,
    tax_resolution: Optional[str] = "inherit",
    tax_line_key: Optional[str] = None,
    tax_category: Optional[str] = "standard",
) -> Optional[str]:
    """Epic #1881 order → selected line key (None = exempt / no tax)."""
    if not profile.lines:
        return None

    resolution = (tax_resolution or "inherit").strip().lower()
    if resolution == "exempt":
        return None
    if resolution == "line":
        key = (tax_line_key or "").strip()
        return key if key and key in profile.lines else None

    legacy_category = tax_category or "standard"
    cat_id = str(category_id).strip() if category_id is not None else ""
    exempt_ids = _as_uuid_str_set(tax_config.get("exempt_menu_category_ids"))
    if cat_id and cat_id in exempt_ids:
        return None

    menu_map = _as_menu_category_line_map(tax_config.get("menu_category_line_map"))
    if cat_id and cat_id in menu_map:
        key = menu_map[cat_id]
        if not key:
            return None
        return key if key in profile.lines else None

    # Dual-read: no menu maps configured yet → legacy fiscal tags.
    if not menu_map and not exempt_ids:
        line = profile.line_for_category(legacy_category)
        return line.key if line else None

    primary = profile.primary_line()
    return primary.key if primary else None


def resolve_product_tax_lines(
    tax_config: Mapping[str, Any],
    *,
    category_id: Any = None,
    tax_resolution: Optional[str] = "inherit",
    tax_line_key: Optional[str] = None,
    tax_category: Optional[str] = "standard",
) -> List[TaxLine]:
    """Applicable lines for a product (#765): honors alternate vs stack modes."""
    profile = resolve_tax_profile(tax_config)
    selected = _resolve_selected_tax_line_key(
        tax_config,
        profile,
        category_id=category_id,
        tax_resolution=tax_resolution,
        tax_line_key=tax_line_key,
        tax_category=tax_category,
    )
    if selected is None:
        return []
    resolution = (tax_resolution or "inherit").strip().lower()
    if resolution == "line":
        line = profile.lines.get(selected)
        return [line] if line else []
    return resolve_applicable_tax_lines(profile, selected_key=selected)


def resolve_product_tax_line(
    tax_config: Mapping[str, Any],
    *,
    category_id: Any = None,
    tax_resolution: Optional[str] = "inherit",
    tax_line_key: Optional[str] = None,
    tax_category: Optional[str] = "standard",
) -> Optional[TaxLine]:
    """Epic #1881 order: override → exempt set → menu map → primary.

    Returns the selected/mapped line (not the full stack). Use
    ``resolve_product_tax_lines`` when amounts must include stacked tributes.
    """
    lines = resolve_product_tax_lines(
        tax_config,
        category_id=category_id,
        tax_resolution=tax_resolution,
        tax_line_key=tax_line_key,
        tax_category=tax_category,
    )
    if not lines:
        return None
    # Prefer the mapped/selected line (last in stack = selected) for bucket labels.
    return lines[-1]


def resolve_effective_tax_category(
    tax_config: Mapping[str, Any],
    *,
    category_id: Any = None,
    tax_resolution: Optional[str] = "inherit",
    tax_line_key: Optional[str] = None,
    tax_category: Optional[str] = "standard",
) -> str:
    """Map resolved line → standard|liquor|exempt for legacy POS/GL buckets.

    When ``category_map.standard`` and ``category_map.liquor`` share the same
    tax line key (common in MX / single-rate packs), do **not** dump every
    taxable item into ``liquor`` — that made receipts show CO "IVA licores 5%"
    for ordinary IVA 16% (#1899). Use the distinct liquor key only when it
    differs from standard; otherwise fall back to the product's fiscal tag.
    """
    line = resolve_product_tax_line(
        tax_config,
        category_id=category_id,
        tax_resolution=tax_resolution,
        tax_line_key=tax_line_key,
        tax_category=tax_category,
    )
    if line is None:
        return "exempt"
    profile = resolve_tax_profile(tax_config)
    standard_key = profile.category_map.get("standard")
    liquor_key = profile.category_map.get("liquor")
    if liquor_key and liquor_key != standard_key and line.key == liquor_key:
        return "liquor"
    legacy = str(tax_category or "standard").strip().lower()
    if legacy == "liquor" and liquor_key and line.key == liquor_key:
        return "liquor"
    return "standard"


def liquor_tax_label_for_config(tax_config: Mapping[str, Any]) -> str:
    """Label for the liquor bucket — tenant tax_lines[].label, never a hard-coded CO string."""
    line = resolve_tax_profile(tax_config).line_for_category("liquor")
    if line and line.label:
        return line.label
    return "IVA licores 5%"


def _normalize_tax_line_mode(raw: Any) -> str:
    mode = str(raw or "primary").strip().lower()
    return mode if mode in TAX_LINE_MODES else "primary"


def _normalize_exclusive_group(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    group = str(raw).strip()
    return group or None


def _line_from_mapping(item: Mapping[str, Any]) -> Optional[TaxLine]:
    key = str(item.get("key") or "").strip()
    if not key:
        return None
    rate = float(item.get("rate") or 0)
    label = str(item.get("label") or key)
    included = bool(item.get("included_in_price", False))
    gl_role = str(item.get("gl_role") or "iva")
    return TaxLine(
        key=key,
        label=label,
        rate=rate,
        included_in_price=included,
        gl_role=gl_role,
        gl_account_id=item.get("gl_account_id"),
        gl_account_code=item.get("gl_account_code"),
        mode=_normalize_tax_line_mode(item.get("mode")),
        exclusive_group=_normalize_exclusive_group(item.get("exclusive_group")),
    )


def _co_columns_active(tax_config: Mapping[str, Any]) -> bool:
    return bool(
        tax_config.get("inc_applicable")
        or tax_config.get("iva_applicable")
        or tax_config.get("liquor_tax_applicable")
    )


def _profile_from_co_columns(tax_config: Mapping[str, Any]) -> TaxProfile:
    lines: Dict[str, TaxLine] = {}
    category_map: Dict[str, Optional[str]] = {
        "standard": None,
        "liquor": None,
        "exempt": None,
    }

    if tax_config.get("inc_applicable"):
        rate = float(tax_config.get("inc_rate") or 0)
        lines["inc"] = TaxLine(
            key="inc",
            label=f"INC {round(rate * 100)}%",
            rate=rate,
            included_in_price=bool(tax_config.get("inc_included_in_price", True)),
            gl_role="inc",
            gl_account_id=tax_config.get("inc_gl_account_id"),
            gl_account_code=tax_config.get("inc_gl_account_code"),
            mode="primary",
            exclusive_group="vat",
        )
        category_map["standard"] = "inc"
    elif tax_config.get("iva_applicable"):
        rate = float(tax_config.get("iva_rate") or 0)
        lines["iva"] = TaxLine(
            key="iva",
            label=f"IVA {round(rate * 100)}%",
            rate=rate,
            included_in_price=bool(tax_config.get("iva_included_in_price", False)),
            gl_role="iva",
            gl_account_id=tax_config.get("iva_gl_account_id"),
            gl_account_code=tax_config.get("iva_gl_account_code"),
            mode="primary",
            exclusive_group="vat",
        )
        category_map["standard"] = "iva"

    if tax_config.get("liquor_tax_applicable"):
        rate = float(tax_config.get("liquor_tax_rate") or 0.05)
        lines["liquor"] = TaxLine(
            key="liquor",
            label="IVA licores 5%" if abs(rate - 0.05) < 1e-9 else f"IVA licores {round(rate * 100)}%",
            rate=rate,
            included_in_price=bool(tax_config.get("liquor_tax_included_in_price", False)),
            gl_role="liquor",
            gl_account_id=tax_config.get("liquor_tax_gl_account_id"),
            gl_account_code=tax_config.get("liquor_tax_gl_account_code"),
            mode="alternate",
            exclusive_group="vat",
        )
        category_map["liquor"] = "liquor"

    return TaxProfile(lines=lines, category_map=category_map)


def sync_co_tax_lines_for_sales_profile(
    tax_config: Mapping[str, Any],
    *,
    iva_applicable: bool,
    inc_applicable: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[str]], Dict[str, Optional[str]]]:
    """Rewrite CO tax_lines/maps when Perfil de ventas flips IVA↔INC (#2031).

    Preserves liquor + custom lines. Remaps menu/category map keys that pointed
    at the old primary (iva↔inc) so POS does not keep stale IVA labels under INC.
    """
    cfg = dict(tax_config)
    cfg["iva_applicable"] = bool(iva_applicable)
    cfg["inc_applicable"] = bool(inc_applicable)
    # Defaults when columns never set (fresh INSERT path).
    if cfg.get("iva_rate") is None:
        cfg["iva_rate"] = 0.19
    if cfg.get("inc_rate") is None:
        cfg["inc_rate"] = 0.08

    profile = _profile_from_co_columns(cfg)
    primary = profile.primary_line()
    primary_key = primary.key if primary else None

    existing = _as_mapping_list(cfg.get("tax_lines"))
    kept: List[Dict[str, Any]] = []
    for item in existing:
        key = str(item.get("key") or "").strip()
        if not key or key in ("iva", "inc"):
            continue
        kept.append(dict(item))

    out_lines: List[Dict[str, Any]] = []
    if primary is not None:
        out_lines.append({
            "key": primary.key,
            "label": primary.label,
            "rate": primary.rate,
            "included_in_price": primary.included_in_price,
            "gl_role": primary.gl_role,
            "mode": "primary",
            "exclusive_group": primary.exclusive_group or "vat",
        })
    out_lines.extend(kept)

    # If columns say liquor on but tax_lines lacked it, seed from columns.
    if cfg.get("liquor_tax_applicable") and not any(
        str(x.get("key") or "") == "liquor" for x in out_lines
    ):
        liquor = profile.lines.get("liquor")
        if liquor is not None:
            out_lines.append({
                "key": liquor.key,
                "label": liquor.label,
                "rate": liquor.rate,
                "included_in_price": liquor.included_in_price,
                "gl_role": liquor.gl_role,
                "mode": "alternate",
                "exclusive_group": liquor.exclusive_group or "vat",
            })

    category_map = {
        "standard": primary_key,
        "liquor": "liquor" if any(str(x.get("key") or "") == "liquor" for x in out_lines) else primary_key,
        "exempt": None,
    }
    raw_map = _as_category_map(cfg.get("category_map"))
    if raw_map.get("exempt") is not None:
        category_map["exempt"] = raw_map.get("exempt")

    menu_map = _as_menu_category_line_map(cfg.get("menu_category_line_map"))
    remapped: Dict[str, Optional[str]] = {}
    for cat_id, line_key in menu_map.items():
        if line_key is None:
            remapped[cat_id] = None
            continue
        if line_key in ("iva", "inc"):
            # Flip to active primary, or clear when non-responsible (#2031).
            remapped[cat_id] = primary_key
        else:
            remapped[cat_id] = line_key

    return out_lines, category_map, remapped


def resolve_applicable_tax_lines(
    profile: TaxProfile,
    *,
    selected_key: Optional[str] = None,
) -> List[TaxLine]:
    """Seed for #765: lines that apply for a mapped/selected key.

    - alternate: selected replaces primary (override)
    - stack: primary + selected when both exist
    - primary / missing mode: selected only (or primary when no selection)
    """
    primary = profile.primary_line()
    key = (selected_key or "").strip()
    if not key:
        return [primary] if primary else []
    selected = profile.lines.get(key)
    if selected is None:
        return [primary] if primary else []

    mode = selected.mode if selected.mode in TAX_LINE_MODES else "primary"
    if mode == "alternate":
        return [selected]
    if mode == "stack":
        out: List[TaxLine] = []
        if primary is not None and primary.key != selected.key:
            out.append(primary)
        out.append(selected)
        return out
    return [selected]


def resolve_tax_profile(tax_config: Mapping[str, Any]) -> TaxProfile:
    """Build a TaxProfile from explicit tax_lines or CO column adapter."""
    raw_lines = _as_mapping_list(tax_config.get("tax_lines"))
    if raw_lines:
        # Commercial disable (#1868): empty profile for commercial-only tenants.
        # CO may persist tax_lines while commercial_tax_applicable stays false.
        if (
            tax_config.get("commercial_tax_applicable") is False
            and not _co_columns_active(tax_config)
        ):
            return TaxProfile(
                lines={},
                category_map={"standard": None, "liquor": None, "exempt": None},
            )
        lines: Dict[str, TaxLine] = {}
        for item in raw_lines:
            line = _line_from_mapping(item)
            if line:
                lines[line.key] = line
        category_map = _as_category_map(tax_config.get("category_map"))
        if not category_map:
            # Default commercial map: standard → first line; liquor → same; exempt → none
            first_key = next(iter(lines), None)
            category_map = {
                "standard": first_key,
                "liquor": first_key,
                "exempt": None,
            }
        else:
            category_map.setdefault("exempt", None)
            category_map.setdefault("standard", next(iter(lines), None))
            category_map.setdefault("liquor", category_map.get("standard"))
        return TaxProfile(lines=lines, category_map=category_map)

    return _profile_from_co_columns(tax_config)


def tax_amount_float(base: float, line: TaxLine) -> float:
    """POS/orders/tip style: whole-unit round after formula."""
    amount = float(base or 0)
    if amount <= 0 or line.rate <= 0:
        return 0.0
    if line.included_in_price:
        return float(round(amount * line.rate / (1 + line.rate)))
    return float(round(amount * line.rate))


def tax_amount_decimal(base: Decimal, line: TaxLine) -> Tuple[Decimal, bool]:
    """GL style: Decimal extractive/additive without intermediate float round.

    Returns (tax_amount, is_additive).
    """
    amount = Decimal(str(base or 0))
    rate = Decimal(str(line.rate or 0))
    if amount <= 0 or rate <= 0:
        return Decimal("0"), False
    if line.included_in_price:
        return amount - (amount / (1 + rate)), False
    return amount * rate, True


def compute_items_tax_totals(
    items_rows: Sequence[Any],
    tax_config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Per-item tax totals honoring alternate/stack + included_in_price (#765)."""
    profile = resolve_tax_profile(tax_config)
    standard_tax = Decimal("0")
    liquor_tax = Decimal("0")
    standard_additive = Decimal("0")
    liquor_additive = Decimal("0")
    standard_gl_role: Optional[str] = None
    liquor_gl_role: Optional[str] = None

    for row in items_rows:
        base = Decimal(str(_row_field(row, "subtotal") or 0))
        if base <= 0:
            continue
        applied = resolve_product_tax_lines(
            tax_config,
            category_id=_row_field(row, "category_id"),
            tax_resolution=_row_field(row, "tax_resolution") or "inherit",
            tax_line_key=_row_field(row, "tax_line_key"),
            tax_category=_row_field(row, "tax_category") or "standard",
        )
        if not applied:
            continue
        bucket = resolve_effective_tax_category(
            tax_config,
            category_id=_row_field(row, "category_id"),
            tax_resolution=_row_field(row, "tax_resolution") or "inherit",
            tax_line_key=_row_field(row, "tax_line_key"),
            tax_category=_row_field(row, "tax_category") or "standard",
        )
        if bucket == "exempt":
            continue
        for line in applied:
            tax, is_additive = tax_amount_decimal(base, line)
            if tax <= 0:
                continue
            if bucket == "liquor":
                liquor_tax += tax
                if is_additive:
                    liquor_additive += tax
                if liquor_gl_role is None:
                    liquor_gl_role = line.gl_role
            else:
                standard_tax += tax
                if is_additive:
                    standard_additive += tax
                if standard_gl_role is None:
                    standard_gl_role = line.gl_role

    primary = profile.primary_line()
    return {
        "standard_tax": standard_tax,
        "liquor_tax": liquor_tax,
        "standard_additive": standard_additive,
        "liquor_additive": liquor_additive,
        "standard_is_additive": standard_additive > 0,
        "liquor_is_additive": liquor_additive > 0,
        "standard_gl_role": standard_gl_role or (primary.gl_role if primary else None),
        "liquor_gl_role": liquor_gl_role,
        "primary_line": primary,
        "profile": profile,
    }


def compute_category_breakdown(
    items_rows: Sequence[Any],
    tax_config: Mapping[str, Any],
) -> Tuple[float, float, str]:
    """Compat wrapper: (standard_tax, liquor_tax, standard_tax_label).

    Dual-reads menu-category maps + product override when present on rows;
    otherwise uses legacy tax_category. Uses per-item alternate/stack resolve (#765)
    with POS-style whole-unit rounding via ``tax_amount_float``.
    """
    profile = resolve_tax_profile(tax_config)
    standard_tax = 0.0
    liquor_tax = 0.0

    for row in items_rows:
        base = float(_row_field(row, "subtotal") or 0)
        if base <= 0:
            continue
        applied = resolve_product_tax_lines(
            tax_config,
            category_id=_row_field(row, "category_id"),
            tax_resolution=_row_field(row, "tax_resolution") or "inherit",
            tax_line_key=_row_field(row, "tax_line_key"),
            tax_category=_row_field(row, "tax_category") or "standard",
        )
        if not applied:
            continue
        bucket = resolve_effective_tax_category(
            tax_config,
            category_id=_row_field(row, "category_id"),
            tax_resolution=_row_field(row, "tax_resolution") or "inherit",
            tax_line_key=_row_field(row, "tax_line_key"),
            tax_category=_row_field(row, "tax_category") or "standard",
        )
        if bucket == "exempt":
            continue
        amount = sum(tax_amount_float(base, line) for line in applied)
        if bucket == "liquor":
            liquor_tax += amount
        else:
            standard_tax += amount

    primary = profile.primary_line()
    label = primary.label if primary else "Impuesto"
    return float(standard_tax), float(liquor_tax), label


def compute_gl_category_taxes(
    standard_subtotal: Decimal,
    liquor_subtotal: Decimal,
    tax_config: Mapping[str, Any],
) -> Dict[str, Any]:
    """GL amounts for standard + liquor category subtotals (cierre fallback).

    Prefer ``compute_items_tax_totals`` when order lines are available so stack
    modes and per-line included flags are exact.
    """
    profile = resolve_tax_profile(tax_config)
    std_line = profile.line_for_category("standard")
    liq_line = profile.line_for_category("liquor")

    standard_tax = Decimal("0")
    liquor_tax = Decimal("0")
    standard_is_additive = False
    liquor_is_additive = False
    standard_gl_role: Optional[str] = None
    liquor_gl_role: Optional[str] = None

    if std_line and standard_subtotal > 0:
        standard_tax, standard_is_additive = tax_amount_decimal(standard_subtotal, std_line)
        standard_gl_role = std_line.gl_role

    if liq_line and liquor_subtotal > 0:
        liquor_tax, liquor_is_additive = tax_amount_decimal(liquor_subtotal, liq_line)
        liquor_gl_role = liq_line.gl_role

    return {
        "standard_tax": standard_tax,
        "liquor_tax": liquor_tax,
        "standard_additive": standard_tax if standard_is_additive else Decimal("0"),
        "liquor_additive": liquor_tax if liquor_is_additive else Decimal("0"),
        "standard_is_additive": standard_is_additive,
        "liquor_is_additive": liquor_is_additive,
        "standard_gl_role": standard_gl_role,
        "liquor_gl_role": liquor_gl_role,
        "primary_line": profile.primary_line(),
        "profile": profile,
    }


def tip_tax_is_additive(tax_config: Mapping[str, Any]) -> bool:
    line = resolve_tax_profile(tax_config).primary_line()
    if not line:
        return False
    return not line.included_in_price


def primary_gl_role(tax_config: Mapping[str, Any]) -> Optional[str]:
    line = resolve_tax_profile(tax_config).primary_line()
    return line.gl_role if line else None


def additive_order_tax_total(
    standard_tax: float,
    liquor_tax: float,
    tax_config: Mapping[str, Any],
) -> float:
    """Tax dollars added on top of product total_amount (cierre debit_total rule).

    Included-in-price standard tax is extractive (already in prices) — omit it.
    Liquor / any non-included bucket line is additive.
    """
    profile = resolve_tax_profile(tax_config)
    std_line = profile.line_for_category("standard")
    liq_line = profile.line_for_category("liquor")
    std_extra = float(standard_tax or 0) if (std_line and not std_line.included_in_price) else 0.0
    # Liquor with no line → 0; included liquor line (rare commercial) → omit
    if liq_line is None:
        liq_extra = 0.0
    elif liq_line.included_in_price:
        liq_extra = 0.0
    else:
        liq_extra = float(liquor_tax or 0)
    return float(std_extra + liq_extra)


def _line_tax_base(line: Mapping[str, Any]) -> float:
    """Same base as POS `_tax_rows_from_evaluated_lines` (post-promo/manual net)."""
    return float(
        line.get("net_total", line.get("subtotal_after_promo", line.get("subtotal", 0))) or 0
    )


def annotate_line_tax_amounts(
    lines: Sequence[Mapping[str, Any]],
    tax_config: Mapping[str, Any],
    *,
    reconcile_to: Optional[Tuple[float, float]] = None,
) -> List[Dict[str, Any]]:
    """Attach per-line tax_amount / tax_label for POS Orden display.

    Mutates dict lines in place when they are mutable mappings; always returns
    the annotated list. When ``reconcile_to`` is (standard_tax, liquor_tax),
    adjusts the last taxable line in each bucket so Σ line taxes match the
    cart-level category rounding. Stack modes sum all applicable tributes (#765).
    """
    out: List[Dict[str, Any]] = []
    bucket_indices: Dict[str, List[int]] = {"standard": [], "liquor": []}

    for idx, raw in enumerate(lines):
        line = dict(raw) if not isinstance(raw, dict) else raw
        base = _line_tax_base(line)
        applied = resolve_product_tax_lines(
            tax_config,
            category_id=line.get("category_id"),
            tax_resolution=line.get("tax_resolution") or "inherit",
            tax_line_key=line.get("tax_line_key"),
            tax_category=line.get("tax_category") or "standard",
        )
        bucket = resolve_effective_tax_category(
            tax_config,
            category_id=line.get("category_id"),
            tax_resolution=line.get("tax_resolution") or "inherit",
            tax_line_key=line.get("tax_line_key"),
            tax_category=line.get("tax_category") or "standard",
        )
        if not applied or base <= 0:
            line["tax_amount"] = 0.0
            line["tax_label"] = None
            line["tax_rate"] = None
            line["included_in_price"] = None
        else:
            amount = sum(tax_amount_float(base, tax_line) for tax_line in applied)
            line["tax_amount"] = float(amount)
            line["tax_label"] = " + ".join(tax_line.label for tax_line in applied)
            line["tax_rate"] = float(applied[-1].rate)
            line["included_in_price"] = all(tax_line.included_in_price for tax_line in applied)
            if bucket in bucket_indices and amount > 0:
                bucket_indices[bucket].append(idx)

        out.append(line)

    if reconcile_to is not None:
        targets = {"standard": float(reconcile_to[0] or 0), "liquor": float(reconcile_to[1] or 0)}
        for bucket, target in targets.items():
            indices = bucket_indices.get(bucket) or []
            if not indices:
                continue
            current = sum(float(out[i].get("tax_amount") or 0) for i in indices)
            delta = round(target - current)
            if delta == 0:
                continue
            last = out[indices[-1]]
            last["tax_amount"] = float(round(float(last.get("tax_amount") or 0) + delta))

    return out
