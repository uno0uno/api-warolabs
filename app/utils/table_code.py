"""Table short codes for POS floor plan display (warocol.com#927)."""
from __future__ import annotations

import re
from typing import Iterable, Optional

_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,4}$")
_DIGITS_RE = re.compile(r"\d+")


def infer_table_code(name: str) -> str:
    """Mirror front tableShortId: first digit run, else first 3 letters uppercased."""
    cleaned = (name or "").strip()
    if not cleaned:
        return "?"
    match = _DIGITS_RE.search(cleaned)
    if match:
        return match.group(0)[:4]
    return cleaned[:3].upper()


def normalize_table_code(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    trimmed = code.strip()
    if not trimmed:
        return None
    if len(trimmed) > 4:
        raise ValueError("Table code must be at most 4 characters")
    if not _CODE_RE.fullmatch(trimmed):
        raise ValueError("Table code must be 1–4 alphanumeric characters")
    return trimmed.upper()


def resolve_unique_code(proposed: str, used: Iterable[str]) -> str:
    """Return proposed code or a suffixed variant when already taken in tenant."""
    base = proposed[:4]
    taken = {c.upper() for c in used if c}
    if base.upper() not in taken:
        return base.upper()

    for suffix in "ABCDEFGHJKLMNPQRSTUVWXYZ":
        if len(base) >= 4:
            candidate = base[:3] + suffix
        else:
            candidate = base + suffix
        if candidate.upper() not in taken:
            return candidate.upper()

    raise ValueError("Unable to assign a unique table code")
