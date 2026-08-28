"""Helpers for table-session covers / custom label (warocol.com#2469)."""
from typing import Optional, Tuple


def guest_snapshot_from_capacity(capacity: Optional[int]) -> Tuple[int, Optional[int]]:
    """Default covers from catalog table capacity; snapshot may be None."""
    cap: Optional[int] = None
    if capacity is not None:
        parsed = int(capacity)
        if parsed >= 1:
            cap = parsed
    covers = cap if cap is not None else 1
    return covers, cap


def normalize_custom_label(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
