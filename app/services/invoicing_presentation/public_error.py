"""Cashier-facing FE error copy — never leak Matias SQL or schema dumps."""
from __future__ import annotations

from typing import Optional

PUBLIC_FACTURADOR_RETRY = (
    "No se pudo emitir la factura electrónica. "
    "El facturador tuvo un error interno. Puedes reintentar."
)

_LEAK_MARKERS = (
    "sqlstate",
    "unknown column",
    "file_managers",
    "select * from",
    "connection: mysql",
    "sql:",
)


def public_invoice_error_message(raw: Optional[str]) -> Optional[str]:
    """Keep DIAN/business errors; replace infrastructure dumps."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return text
    lower = text.lower()
    if any(marker in lower for marker in _LEAK_MARKERS):
        return PUBLIC_FACTURADOR_RETRY
    if "matias api 5" in lower:
        return PUBLIC_FACTURADOR_RETRY
    return text
