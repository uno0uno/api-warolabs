"""
POS receipt email template.
Sent to the customer immediately after a POS order is completed, if receipt_email was provided.
Plain text format — short and scannable, like a physical receipt.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional

BOGOTA_TZ = ZoneInfo("America/Bogota")

_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

_PAYMENT_LABELS = {
    "cash": "Efectivo",
    "card": "Tarjeta",
    "digital": "Pago digital",
    "credit": "Crédito",
}


def _format_cop(amount: float) -> str:
    return f"${amount:,.0f}".replace(",", ".")


def _format_bogota_date(dt: datetime) -> str:
    local = dt.astimezone(BOGOTA_TZ)
    month = _MONTHS_ES[local.month - 1]
    hour = local.hour % 12 or 12
    ampm = "a. m." if local.hour < 12 else "p. m."
    return f"{local.day} de {month} de {local.year}, {hour}:{local.minute:02d} {ampm}"


def get_pos_receipt_subject(order_number: int, business_name: Optional[str] = None) -> str:
    name = business_name or "WARO"
    return f"Recibo de compra #{order_number} — {name}"


def get_pos_receipt_text(
    order_number: int,
    total_amount: float,
    payment_method: str,
    items: List[Dict[str, Any]],
    order_date: datetime,
    business_name: Optional[str] = None,
    business_address: Optional[str] = None,
    business_city: Optional[str] = None,
    business_phone: Optional[str] = None,
) -> str:
    date_str = _format_bogota_date(order_date)
    payment_label = _PAYMENT_LABELS.get(payment_method, payment_method)
    name = business_name or "WARO"

    # Business header
    header_lines = [name]
    if business_address:
        addr = business_address
        if business_city:
            addr += f", {business_city}"
        header_lines.append(addr)
    if business_phone:
        header_lines.append(f"Tel: {business_phone}")
    header_block = "\n".join(header_lines)

    items_lines = []
    for item in items:
        qty = item.get("quantity", 1)
        name_item = item.get("product", {}).get("name", "Producto")
        subtotal = float(item.get("subtotal", 0))
        line = f"  {qty}x {name_item}  {_format_cop(subtotal)}"
        if item.get("modifiers"):
            mod_names = ", ".join(
                m.get("name", "") for m in item["modifiers"] if m.get("name")
            )
            if mod_names:
                line += f"\n     + {mod_names}"
        items_lines.append(line)
    items_block = "\n".join(items_lines)

    return f"""\
{header_block}
================================
Orden #{order_number}
Fecha: {date_str}
--------------------------------
PRODUCTOS
{items_block}
--------------------------------
TOTAL: {_format_cop(total_amount)}
Método de pago: {payment_label}
================================

Gracias por tu compra.
"""
