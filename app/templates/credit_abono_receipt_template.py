"""
Credit abono (cartera payment) receipt email — plain text, like POS receipt emails.
"""
from typing import Any, Dict, List, Optional

from app.core.localization import format_money, get_translator, normalize_locale


def get_credit_abono_subject(business_name: Optional[str], locale: str = "es") -> str:
    _ = get_translator(normalize_locale(locale))
    brand = (business_name or "").strip() or _("Your business")
    return _("Credit payment receipt — {business}", business=brand)


def get_credit_abono_text(
    customer_name: str,
    payment_date_label: str,
    payment_method_label: str,
    total_amount: float,
    lines: List[Dict[str, Any]],
    notes: Optional[str] = None,
    total_outstanding_after: Optional[float] = None,
    business_name: Optional[str] = None,
    business_address: Optional[str] = None,
    business_city: Optional[str] = None,
    business_phone: Optional[str] = None,
    locale: str = "es",
    currency_code: str = "COP",
) -> str:
    _ = get_translator(normalize_locale(locale))
    out: List[str] = []

    if business_name:
        out.append(business_name.strip())
    if business_address:
        out.append(business_address.strip())
    if business_city:
        out.append(business_city.strip())
    if business_phone:
        out.append(business_phone.strip())
    if out:
        out.append("")

    out.append(_("CREDIT PAYMENT RECEIPT"))
    out.append("=" * 32)
    out.append("")
    out.append(_("Customer: {name}", name=customer_name.strip() or _("Customer")))
    out.append(_("Date: {date}", date=payment_date_label))
    out.append(_("Payment method: {method}", method=payment_method_label))
    out.append("")

    if len(lines) > 1:
        out.append(_("Applied to orders:"))
        for line in lines:
            order_no = line.get("order_number")
            amt = float(line.get("amount") or 0)
            remaining = float(line.get("remaining_amount") or 0)
            out.append(
                _("  Order #{order} — paid {paid} — remaining {remaining}",
                  order=order_no,
                  paid=format_money(amt, currency_code=currency_code, locale=locale),
                  remaining=format_money(remaining, currency_code=currency_code, locale=locale),
                )
            )
        out.append("")
    elif lines:
        line = lines[0]
        order_no = line.get("order_number")
        if order_no:
            out.append(_("Order #{order}", order=order_no))
        remaining = float(line.get("remaining_amount") or 0)
        out.append(
            _("Remaining balance: {amount}",
              amount=format_money(remaining, currency_code=currency_code, locale=locale),
            )
        )
        out.append("")

    out.append(
        _("Total paid: {amount}",
          amount=format_money(total_amount, currency_code=currency_code, locale=locale),
        )
    )

    if total_outstanding_after is not None:
        out.append(
            _("Total outstanding after payment: {amount}",
              amount=format_money(
                  float(total_outstanding_after),
                  currency_code=currency_code,
                  locale=locale,
              ),
            )
        )

    if notes and str(notes).strip():
        out.append("")
        out.append(_("Notes: {notes}", notes=str(notes).strip()))

    out.append("")
    out.append(_("Thank you for your payment."))
    return "\n".join(out)
