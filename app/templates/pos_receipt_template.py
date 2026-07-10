"""
POS receipt email template.
Sent to the customer immediately after a POS order is completed, if receipt_email was provided.
Plain text format — short and scannable, like a physical receipt.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional

from app.core.platform_legal import waro_platform_footer_text

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

_WARO_TYPE_LABELS = {
    "points_cop": "Canje WaRo (puntos)",
    "reward_fixed_cop": "Canje WaRo",
    "reward_free_product": "Canje WaRo (producto gratis)",
}


def _waro_line_label(entry: Dict[str, Any]) -> str:
    reward_name = entry.get("reward_name")
    if reward_name:
        return f"Canje WaRo ({reward_name})"
    redemption_type = entry.get("redemption_type") or ""
    return _WARO_TYPE_LABELS.get(redemption_type, "Canje WaRo")


def _format_cop(amount: float) -> str:
    return f"${amount:,.0f}".replace(",", ".")


def _format_bogota_date(dt: datetime) -> str:
    local = dt.astimezone(BOGOTA_TZ)
    month = _MONTHS_ES[local.month - 1]
    hour = local.hour % 12 or 12
    ampm = "a. m." if local.hour < 12 else "p. m."
    return f"{local.day} de {month} de {local.year}, {hour}:{local.minute:02d} {ampm}"


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _append_party(lines: List[str], title: str, party: Optional[Dict[str, Any]]) -> None:
    if not party:
        return
    name = _clean(party.get("name"))
    fiscal_id = _clean(party.get("fiscal_id"))
    if not name and not fiscal_id:
        return
    lines.append(title)
    if name:
        lines.append(f"  {name}")
    if fiscal_id:
        id_type = _clean(party.get("fiscal_id_type"))
        label = f"{id_type}: {fiscal_id}" if id_type else f"ID: {fiscal_id}"
        lines.append(f"  {label}")
    address = _clean(party.get("address"))
    city = _clean(party.get("city"))
    if address:
        lines.append(f"  {address}{', ' + city if city else ''}")
    email = _clean(party.get("email"))
    if email:
        lines.append(f"  Email: {email}")


def get_pos_receipt_subject(
    order_number: int,
    business_name: Optional[str] = None,
    invoice_prefix: Optional[str] = None,
    invoice_number: Optional[int] = None,
) -> str:
    name = business_name or "WARO"
    if invoice_prefix and invoice_number:
        return f"Factura electrónica {invoice_prefix}-{invoice_number} — {name}"
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
    invoice_presentation: Optional[Dict[str, Any]] = None,
    tip_amount: float = 0.0,
    tip_label: str = "Propina",
    promo_savings: float = 0.0,
    promo_breakdown: Optional[List[Dict[str, Any]]] = None,
    waro_redemption_summary: Optional[Dict[str, Any]] = None,
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
    _promo_breakdown = promo_breakdown or []
    _waro = waro_redemption_summary or {}
    _waro_breakdown = _waro.get("waro_breakdown") or []
    _waro_discount = float(_waro.get("waro_discount_cop") or 0)
    _has_promo = promo_savings > 0 or len(_promo_breakdown) > 0
    _has_waro = _waro_discount > 0 or len(_waro_breakdown) > 0
    _show_subtotal = subtotal > 0 and (
        discount_amount > 0 or _has_promo or _has_waro
    )
    if _show_subtotal:
        totals_lines.append(f"Subtotal: {_format_cop(subtotal)}")
    if _promo_breakdown:
        for promo in _promo_breakdown:
            label = promo.get("promotion_name") or "Promoción"
            savings = float(promo.get("savings") or 0)
            if savings > 0:
                totals_lines.append(f"{label}: -{_format_cop(savings)}")
    elif promo_savings > 0:
        totals_lines.append(f"Promoción: -{_format_cop(promo_savings)}")
    if discount_amount > 0:
        totals_lines.append(f"Descuento manual: -{_format_cop(discount_amount)}")
    if _waro_breakdown:
        for entry in _waro_breakdown:
            cop = float(entry.get("cop_discount") or 0)
            if cop > 0:
                totals_lines.append(f"{_waro_line_label(entry)}: -{_format_cop(cop)}")
    elif _waro_discount > 0:
        totals_lines.append(f"Canje WaRo: -{_format_cop(_waro_discount)}")
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
        presentation = invoice_presentation or {}
        dian_url = (
            _clean(presentation.get("dian_url"))
            or f"https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey={invoice_cufe}"
        )
        invoice_lines = [
            "================================",
            "FACTURA ELECTRÓNICA DE VENTA",
            "Representación gráfica para verificación contable.",
            f"Número: {invoice_prefix}-{invoice_number}",
        ]
        status = _clean(presentation.get("status"))
        if status:
            invoice_lines.append(f"Estado DIAN: {status}")
        emitted_at = presentation.get("emitted_at")
        if isinstance(emitted_at, datetime):
            invoice_lines.append(f"Emisión: {_format_bogota_date(emitted_at)}")
        elif _clean(emitted_at):
            invoice_lines.append(f"Emisión: {_clean(emitted_at)}")

        _append_party(invoice_lines, "Emisor:", presentation.get("issuer"))
        _append_party(invoice_lines, "Adquirente:", presentation.get("acquirer"))

        resolution = presentation.get("resolution") or {}
        if resolution:
            res_lines = []
            number = _clean(resolution.get("number"))
            prefix = _clean(resolution.get("prefix"))
            from_number = resolution.get("from_number")
            to_number = resolution.get("to_number")
            date_from = _clean(resolution.get("date_from"))
            date_to = _clean(resolution.get("date_to"))
            if number:
                res_lines.append(f"Resolución DIAN: {number}")
            if prefix or from_number or to_number:
                res_lines.append(f"Rango: {prefix or invoice_prefix} {from_number or ''}-{to_number or ''}".strip())
            if date_from or date_to:
                res_lines.append(f"Vigencia: {date_from or 'N/D'} a {date_to or 'N/D'}")
            invoice_lines.extend(res_lines)

        tax_details = presentation.get("tax_details") or []
        if tax_details:
            invoice_lines.append("Impuestos:")
            for tax in tax_details:
                label = _clean(tax.get("label")) or "Impuesto"
                amount = float(tax.get("amount") or 0)
                base = tax.get("base")
                base_text = f" base {_format_cop(float(base))}" if base is not None else ""
                invoice_lines.append(f"  {label}{base_text}: {_format_cop(amount)}")

        attachment_status = presentation.get("attachments") or {}
        attachment_lines = []
        if attachment_status.get("pdf"):
            attachment_lines.append("PDF adjunto")
        if attachment_status.get("xml"):
            attachment_lines.append("XML adjunto")
        if attachment_lines:
            invoice_lines.append("Archivos: " + ", ".join(attachment_lines))
        elif attachment_status.get("xml") is False and not attachment_status.get("pdf"):
            # PDF disabled env: do not alarm about missing graphic PDF
            invoice_lines.append("Archivos: XML fiscal cuando esté disponible; PDF gráfico no se envía.")

        invoice_lines.extend([
            f"CUFE: {invoice_cufe}",
            f"Verificar en DIAN: {dian_url}",
            "================================",
        ])
        invoice_block = "\n" + "\n".join(invoice_lines)

    has_fe = bool(invoice_prefix and invoice_number and invoice_cufe)
    if not has_fe:
        sale_notice = (
            "\n--------------------------------\n"
            "COMPROBANTE DE VENTA\n"
            "No es factura electrónica DIAN\n"
        )
        if business_name:
            sale_notice += f"Vendedor: {business_name}\n"
    else:
        sale_notice = ""

    platform_footer = waro_platform_footer_text(with_fe_note=has_fe)

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
{sale_notice}{invoice_block}
{platform_footer}"""
