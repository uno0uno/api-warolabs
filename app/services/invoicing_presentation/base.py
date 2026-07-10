"""Protocol for facturador-specific invoice presentation builders."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class InvoicePresentationBuilder(Protocol):
    def build(
        self,
        *,
        invoice_row: Any,
        order_row: Any,
        fiscal_row: Any,
        public_profile: Any = None,
        resolution_row: Any = None,
        tax_details: Optional[List[Dict[str, Any]]] = None,
        serialize_datetimes: bool = False,
    ) -> Dict[str, Any]:
        ...
