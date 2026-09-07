"""
Wallet recharge receipt email — plain text, like credit abono receipts.
"""
from typing import Optional

from app.core.localization import format_money, get_translator, normalize_locale


def get_wallet_recharge_subject(business_name: Optional[str], locale: str = "es") -> str:
    _ = get_translator(normalize_locale(locale))
    brand = (business_name or "").strip() or _("Your business")
    return _("Wallet recharge receipt — {business}", business=brand)


def get_wallet_recharge_text(
    customer_name: str,
    recharge_date_label: str,
    payment_method_label: str,
    amount_cop: float,
    balance_after_cop: float,
    notes: Optional[str] = None,
    business_name: Optional[str] = None,
    business_address: Optional[str] = None,
    business_city: Optional[str] = None,
    business_phone: Optional[str] = None,
    locale: str = "es",
    currency_code: str = "COP",
) -> str:
    _ = get_translator(normalize_locale(locale))
    out: list[str] = []

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

    out.append(_("WALLET RECHARGE RECEIPT"))
    out.append("=" * 32)
    out.append("")
    out.append(_("Customer: {name}", name=customer_name.strip() or _("Customer")))
    out.append(_("Date: {date}", date=recharge_date_label))
    out.append(_("Payment method: {method}", method=payment_method_label))
    out.append("")
    out.append(
        _("Amount recharged: {amount}",
          amount=format_money(amount_cop, currency_code=currency_code, locale=locale),
        )
    )
    out.append(
        _("Balance after recharge: {amount}",
          amount=format_money(balance_after_cop, currency_code=currency_code, locale=locale),
        )
    )

    if notes and str(notes).strip():
        out.append("")
        out.append(_("Notes: {notes}", notes=str(notes).strip()))

    out.append("")
    out.append(_("Thank you for your payment."))
    return "\n".join(out)
