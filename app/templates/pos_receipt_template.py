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
    discount_amount: float = 0.0,
    subtotal: float = 0.0,
    standard_tax: float = 0.0,
    liquor_tax: float = 0.0,
    standard_tax_label: str = "Impuesto",
    invoice_prefix: Optional[str] = None,
    invoice_number: Optional[int] = None,
    invoice_cufe: Optional[str] = None,
    tip_amount: float = 0.0,
    tip_label: str = "Propina",
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

    # Build totals block
    totals_lines = []
    if discount_amount > 0 and subtotal > 0:
        totals_lines.append(f"Subtotal: {_format_cop(subtotal)}")
        totals_lines.append(f"Descuento: -{_format_cop(discount_amount)}")
    if standard_tax > 0:
        totals_lines.append(f"{standard_tax_label}: {_format_cop(standard_tax)}")
    if liquor_tax > 0:
        totals_lines.append(f"IVA licores 5%: {_format_cop(liquor_tax)}")
    totals_block = ("\n".join(totals_lines) + "\n") if totals_lines else ""

    # warocol.com#637 — tip line shown separately from the order total so the
    # customer can see exactly how much went to the waiter. Charged total is
    # only printed when there is a tip to avoid noise on the typical receipt.
    tip_line_label = (tip_label or "Propina").strip()[:40] or "Propina"
    tip_block = ""
    if tip_amount > 0:
        charged_total = total_amount + tip_amount
        tip_block = (
            f"{tip_line_label}: {_format_cop(tip_amount)}\n"
            f"--------------------------------\n"
            f"TOTAL COBRADO: {_format_cop(charged_total)}\n"
        )

    # DIAN invoice section (optional)
    invoice_block = ""
    if invoice_prefix and invoice_number and invoice_cufe:
        dian_url = f"https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey={invoice_cufe}"
        invoice_block = f"""
================================
FACTURA ELECTRÓNICA
{invoice_prefix}-{invoice_number}
CUFE: {invoice_cufe}
Verificar: {dian_url}
================================"""

    return f"""\
{header_block}
================================
Orden #{order_number}
Fecha: {date_str}
--------------------------------
PRODUCTOS
{items_block}
--------------------------------
{totals_block}TOTAL: {_format_cop(total_amount)}
{tip_block}Método de pago: {payment_label}
================================

Gracias por tu compra.
{invoice_block}"""
