"""Safe casting for asyncpg.Record / Mapping / raw values used in FE presentation."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional
from uuid import UUID


def row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column from dict, Mapping, or asyncpg.Record without raising."""
    if row is None:
        return default
    if isinstance(row, Mapping) or hasattr(row, "get"):
        try:
            value = row.get(key, default)  # type: ignore[union-attr]
        except Exception:
            value = default
        return default if value is None and default is not None else value
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def clean_str(value: Any) -> Optional[str]:
    """Strip and coerce to non-empty str; UUID/int → str; empty → None."""
    if value is None:
        return None
    if isinstance(value, UUID):
        text = str(value)
    elif isinstance(value, (bytes, bytearray)):
        try:
            text = value.decode("utf-8")
        except Exception:
            return None
    else:
        text = str(value).strip()
    text = text.strip() if text else ""
    return text or None


def as_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def date_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = clean_str(value)
    return text


def datetime_iso(value: Any) -> Optional[str]:
    """Serialize datetime for JSON; leave datetime objects for email template paths."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return clean_str(value)


def keep_datetime_or_iso(value: Any, *, serialize: bool) -> Any:
    """
    Email template may want real datetime for Bogotá formatting;
    API JSON responses want ISO strings.
    """
    if value is None:
        return None
    if not serialize and isinstance(value, datetime):
        return value
    return datetime_iso(value)
