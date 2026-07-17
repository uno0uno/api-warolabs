"""
POS receipt email template.
Sent to the customer immediately after a POS order is completed, if receipt_email was provided.
Plain text format — short and scannable, like a physical receipt.
"""
from datetime import datetime
from typing import List, Dict, Any, Optional

from app.core.localization import (
    format_datetime,
    format_money,
    get_translator,
    normalize_locale,
)
from app.core.platform_legal import waro_platform_footer_text

_PAYMENT_LABEL_MSGIDS = {
    "cash": "Cash",
    "card": "Card",
    "digital": "Digital payment",
    "credit": "Credit",
    "customer_wallet": "Customer wallet",
}

_WARO_TYPE_LABEL_MSGIDS = {
    "points_cop": "WaRo redemption (points)",
    "reward_fixed_cop": "WaRo redemption",
    "reward_free_product": "WaRo redemption (free product)",
}


def _tr(_, msgid: str, **kwargs: Any) -> str:
    text = _(msgid)
    return text.format(**kwargs) if kwargs else text


def _payment_label(payment_method: str, _) -> str:
    msgid = _PAYMENT_LABEL_MSGIDS.get(payment_method)
    return _(msgid) if msgid else payment_method


def _waro_line_label(entry: Dict[str, Any], _) -> str:
    reward_name = entry.get("reward_name")
    if reward_name:
        return _tr(_, "WaRo redemption ({reward_name})", reward_name=reward_name)
    redemption_type = entry.get("redemption_type") or ""
    return _(_WARO_TYPE_LABEL_MSGIDS.get(redemption_type, "WaRo redemption"))


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _append_party(lines: List[str], title: str, party: Optional[Dict[str, Any]], _) -> None:
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
        lines.append(f"  {_tr(_, 'Email: {email}', email=email)}")


def get_pos_receipt_subject(
    order_number: int,
    business_name: Optional[str] = None,
    invoice_prefix: Optional[str] = None,
    invoice_number: Optional[int] = None,
    locale: Optional[str] = None,
) -> str:
    _ = get_translator(locale)
    name = business_name or "WARO"
    if invoice_prefix and invoice_number:
        return _tr(
            _,
            "Electronic invoice {invoice_prefix}-{invoice_number} — {business_name}",
            invoice_prefix=invoice_prefix,
            invoice_number=invoice_number,
            business_name=name,
        )
    return _tr(_, "Purchase receipt #{order_number} — {business_name}", order_number=order_number, business_name=name)


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
    tip_label: Optional[str] = None,
    promo_savings: float = 0.0,
    promo_breakdown: Optional[List[Dict[str, Any]]] = None,
    waro_redemption_summary: Optional[Dict[str, Any]] = None,
    locale: Optional[str] = None,
    currency_code: Optional[str] = None,
    timezone: Optional[str] = None,
) -> str:
    lang = normalize_locale(locale)
    _ = get_translator(lang)
    money = lambda amount: format_money(amount, lang, currency_code)
    date_str = format_datetime(order_date, lang, timezone)
    payment_label = _payment_label(payment_method, _)
    name = business_name or "WARO"

    # Business header
    header_lines = [name]
    if business_address:
        addr = business_address
        if business_city:
            addr += f", {business_city}"
        header_lines.append(addr)
    if business_phone:
        header_lines.append(_tr(_, "Phone: {phone}", phone=business_phone))
    header_block = "\n".join(header_lines)

    items_lines = []
    for item in items:
        qty = item.get("quantity", 1)
        name_item = item.get("product", {}).get("name", _("Product"))
        item_subtotal = float(item.get("subtotal", 0))
        line = f"  {qty}x {name_item}  {money(item_subtotal)}"
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
        totals_lines.append(f"{_('Subtotal')}: {money(subtotal)}")
    if _promo_breakdown:
        for promo in _promo_breakdown:
            label = promo.get("promotion_name") or _("Promotion")
            savings = float(promo.get("savings") or 0)
            if savings > 0:
                totals_lines.append(f"{label}: -{money(savings)}")
    elif promo_savings > 0:
        totals_lines.append(f"{_('Promotion')}: -{money(promo_savings)}")
    if discount_amount > 0:
        totals_lines.append(f"{_('Manual discount')}: -{money(discount_amount)}")
    if _waro_breakdown:
        for entry in _waro_breakdown:
            cop = float(entry.get("cop_discount") or 0)
            if cop > 0:
                totals_lines.append(f"{_waro_line_label(entry, _)}: -{money(cop)}")
    elif _waro_discount > 0:
        totals_lines.append(f"{_('WaRo redemption')}: -{money(_waro_discount)}")
    if standard_tax > 0:
        tax_label = standard_tax_label
        if not tax_label or str(tax_label).strip().lower() in {"impuesto", "tax"}:
            tax_label = _("Tax")
        totals_lines.append(f"{tax_label}: {money(standard_tax)}")
    if liquor_tax > 0:
        totals_lines.append(f"{_('Liquor VAT 5%')}: {money(liquor_tax)}")
    totals_block = ("\n".join(totals_lines) + "\n") if totals_lines else ""

    # warocol.com#637 — tip line shown separately from the order total so the
    # customer can see exactly how much went to the waiter. Charged total is
    # only printed when there is a tip to avoid noise on the typical receipt.
    tip_line_label = (tip_label or _("Tip")).strip()[:40] or _("Tip")
    tip_block = ""
    if tip_amount > 0:
        charged_total = total_amount + tip_amount
        tip_block = (
            f"{tip_line_label}: {money(tip_amount)}\n"
            f"--------------------------------\n"
            f"{_('TOTAL CHARGED')}: {money(charged_total)}\n"
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
            _("ELECTRONIC SALES INVOICE"),
            _("Graphic representation for accounting verification."),
            _tr(_, "Number: {invoice_id}", invoice_id=f"{invoice_prefix}-{invoice_number}"),
        ]
        status = _clean(presentation.get("status"))
        if status:
            invoice_lines.append(_tr(_, "DIAN status: {status}", status=status))
        emitted_at = presentation.get("emitted_at")
        if isinstance(emitted_at, datetime):
            invoice_lines.append(_tr(_, "Issued: {date}", date=format_datetime(emitted_at, lang, timezone)))
        elif _clean(emitted_at):
            invoice_lines.append(_tr(_, "Issued: {date}", date=_clean(emitted_at)))

        _append_party(invoice_lines, _("Issuer:"), presentation.get("issuer"), _)
        _append_party(invoice_lines, _("Buyer:"), presentation.get("acquirer"), _)

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
                res_lines.append(_tr(_, "DIAN resolution: {number}", number=number))
            if prefix or from_number or to_number:
                range_text = f"{prefix or invoice_prefix} {from_number or ''}-{to_number or ''}".strip()
                res_lines.append(_tr(_, "Range: {range}", range=range_text))
            if date_from or date_to:
                res_lines.append(_tr(
                    _,
                    "Valid from {date_from} to {date_to}",
                    date_from=date_from or _("N/A"),
                    date_to=date_to or _("N/A"),
                ))
            invoice_lines.extend(res_lines)

        tax_details = presentation.get("tax_details") or []
        if tax_details:
            invoice_lines.append(_("Taxes:"))
            for tax in tax_details:
                label = _clean(tax.get("label")) or _("Tax")
                amount = float(tax.get("amount") or 0)
                base = tax.get("base")
                base_text = _tr(_, " base {base}", base=money(float(base))) if base is not None else ""
                invoice_lines.append(f"  {label}{base_text}: {money(amount)}")

        attachment_status = presentation.get("attachments") or {}
        attachment_lines = []
        if attachment_status.get("pdf"):
            attachment_lines.append(_("PDF attached"))
        if attachment_status.get("xml"):
            attachment_lines.append(_("XML attached"))
        if attachment_lines:
            invoice_lines.append(_tr(_, "Files: {files}", files=", ".join(attachment_lines)))
        elif attachment_status.get("xml") is False and not attachment_status.get("pdf"):
            invoice_lines.append(_("Files: PDF/XML not yet available in the fiscal repository."))

        invoice_lines.extend([
            f"CUFE: {invoice_cufe}",
            _tr(_, "Verify in DIAN: {url}", url=dian_url),
            "================================",
        ])
        invoice_block = "\n" + "\n".join(invoice_lines)

    has_fe = bool(invoice_prefix and invoice_number and invoice_cufe)
    if not has_fe:
        sale_notice = (
            "\n--------------------------------\n"
            f"{_('SALE RECEIPT')}\n"
            f"{_('Not a DIAN electronic invoice')}\n"
        )
        if business_name:
            sale_notice += f"{_tr(_, 'Seller: {business_name}', business_name=business_name)}\n"
    else:
        sale_notice = ""

    platform_footer = waro_platform_footer_text(with_fe_note=has_fe, locale=lang)

    return f"""\
{header_block}
================================
{_tr(_, "Order #{order_number}", order_number=order_number)}
{_tr(_, "Date: {date}", date=date_str)}
--------------------------------
{_("PRODUCTS")}
{items_block}
--------------------------------
{totals_block}{_('TOTAL')}: {money(total_amount)}
{tip_block}{_tr(_, "Payment method: {payment_label}", payment_label=payment_label)}
================================

{_("Thank you for your purchase.")}
{sale_notice}{invoice_block}
{platform_footer}"""
