"""Factory: pick presentation builder by facturador provider key."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.invoicing_presentation.matias import MatiasPresentationBuilder

_DEFAULT_PROVIDER = "matias"

_BUILDERS = {
    "matias": MatiasPresentationBuilder(),
}


def get_presentation_builder(provider: str = _DEFAULT_PROVIDER) -> MatiasPresentationBuilder:
    key = (provider or _DEFAULT_PROVIDER).strip().lower()
    builder = _BUILDERS.get(key)
    if builder is None:
        # Unknown facturador → safe Matias-compatible fiscal-only path
        return _BUILDERS[_DEFAULT_PROVIDER]
    return builder


def build_invoice_presentation(
    *,
    invoice_row: Any,
    order_row: Any,
    fiscal_row: Any,
    public_profile: Any = None,
    resolution_row: Any = None,
    tax_details: Optional[List[Dict[str, Any]]] = None,
    provider: str = _DEFAULT_PROVIDER,
    serialize_datetimes: bool = False,
) -> Dict[str, Any]:
    """
    Build FE presentation dict for print/email/API.

    serialize_datetimes=True → ISO strings (JSON API).
    serialize_datetimes=False → keep datetime for email templates.
    """
    builder = get_presentation_builder(provider)
    return builder.build(
        invoice_row=invoice_row,
        order_row=order_row,
        fiscal_row=fiscal_row,
        public_profile=public_profile,
        resolution_row=resolution_row,
        tax_details=tax_details,
        serialize_datetimes=serialize_datetimes,
    )
