"""
Magic link email template for FastAPI authentication system.
Compatible with warolabs.com template structure and branding.

Localized via the same stack as the POS receipt
(app.core.localization.get_translator + babel_locale) so the copy and
<html lang> follow the tenant/user locale instead of being hardcoded Spanish.
"""
from typing import Literal, Optional

from app.core.localization import babel_locale, get_translator, normalize_locale
from app.core.platform_legal import waro_platform_footer_text

EmailPurpose = Literal["login", "registration"]

# English msgids — translated in app/locales/{es,en}/LC_MESSAGES/messages.po.
_MSGID_INTRO_LOGIN = "You requested access to your account on {brand_name}. Click the link below to sign in securely:"
_MSGID_INTRO_REGISTRATION = "You started your registration on {brand_name}. Click the link below to complete it securely:"
_MSGID_BUTTON_LOGIN = "🔑 Access my account"
_MSGID_BUTTON_REGISTRATION = "✨ Complete my registration"
_MSGID_CODE_LABEL = "Or use this verification code:"
_MSGID_VALIDITY = "This link is valid for 15 minutes and can only be used once."
_MSGID_IGNORE = "If you did not request this link, you can safely ignore this email."
_MSGID_HELLO = "Hello!"
_MSGID_SALUTATION = "Greetings from {tenant_name}'s ship."
_MSGID_FOUNDER = "Founder of {tenant_name}"
_MSGID_EMAIL_LABEL = "Email: {email}"
_MSGID_SUBJECT_LOGIN = "🔑 Your access to {brand_name} is ready"
_MSGID_SUBJECT_REGISTRATION = "✨ Complete your registration on {brand_name}"


def _tr(_, msgid: str, **kwargs) -> str:
    text = _(msgid)
    return text.format(**kwargs) if kwargs else text


def get_magic_link_template(
    magic_link_url: str,
    verification_code: str,
    tenant_context: dict,
    purpose: EmailPurpose = "login",
    locale: Optional[str] = None,
) -> str:
    """
    Generate magic link email template with dynamic tenant branding and locale.

    Args:
        magic_link_url: The complete magic link URL for authentication
        verification_code: 6-digit verification code
        tenant_context: Tenant configuration with branding information
        purpose: "login" (default) or "registration" — selects subject/body copy
        locale: Tenant/user locale (es|en); defaults to Spanish via normalize_locale

    Returns:
        HTML email template string
    """
    lang = normalize_locale(locale)
    _ = get_translator(lang)
    html_lang = babel_locale(lang)

    brand_name = tenant_context.get("brand_name", "Waro Colombia")
    tenant_name = tenant_context.get("tenant_name", "Waro Colombia")
    admin_name = tenant_context.get("admin_name", "")
    admin_email = tenant_context.get("admin_email", "")

    if purpose == "registration":
        intro = _tr(_, _MSGID_INTRO_REGISTRATION, brand_name=brand_name)
        button_label = _(_MSGID_BUTTON_REGISTRATION)
    else:
        intro = _tr(_, _MSGID_INTRO_LOGIN, brand_name=brand_name)
        button_label = _(_MSGID_BUTTON_LOGIN)

    validity = _(_MSGID_VALIDITY)
    ignore_note = _(_MSGID_IGNORE)
    hello = _(_MSGID_HELLO)
    salutation = _tr(_, _MSGID_SALUTATION, tenant_name=tenant_name)
    code_label = _(_MSGID_CODE_LABEL)
    footer = waro_platform_footer_text(locale=lang)

    # Signature block — rendered only when the contact data is supplied by the
    # caller (tenant_context), never hardcoded to a specific city/phone.
    sig_lines = []
    if admin_name:
        sig_lines.append(admin_name)
        sig_lines.append(_tr(_, _MSGID_FOUNDER, tenant_name=tenant_name))
    if admin_email:
        sig_lines.append(_tr(_, _MSGID_EMAIL_LABEL, email=admin_email))
    signature = "\n".join(f"{line}<br>" for line in sig_lines)

    return f"""
<!DOCTYPE html>
<html lang="{html_lang}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_tr(_, _MSGID_SUBJECT_LOGIN if purpose == "login" else _MSGID_SUBJECT_REGISTRATION, brand_name=brand_name)}</title>
</head>
<body style="font-family: Arial, sans-serif; color: black; margin: 0; padding: 0; text-align: left;">
    <div style="font-family: Arial, sans-serif; max-width: 600px; padding: 20px; border: 1px solid #ddd; border-radius: 8px;">
        <p>{hello}</p>

        <p>{intro}</p>

        <p><a href="{magic_link_url}" style="color: black; background-color: #f0f0f0; padding: 10px; border-radius: 4px; text-decoration: none; display: inline-block;">{button_label}</a></p>

        <p>{code_label}</p>
        <p style="font-size: 28px; font-weight: bold; letter-spacing: 4px; color: #333; background-color: #f8f8f8; padding: 15px; border-radius: 8px; text-align: center; margin: 20px 0;">{verification_code}</p>

        <p>{validity}</p>

        <p>{ignore_note}</p>

        <p>{salutation}</p>

        <br><br>
        ----<br>
        {signature}
        {footer}
    </div>
</body>
</html>
    """.strip()


def get_magic_link_subject(brand_name: str, purpose: EmailPurpose = "login", locale: Optional[str] = None) -> str:
    """Generate email subject line for magic link, localized."""
    _ = get_translator(locale)
    if purpose == "registration":
        return _tr(_, _MSGID_SUBJECT_REGISTRATION, brand_name=brand_name)
    return _tr(_, _MSGID_SUBJECT_LOGIN, brand_name=brand_name)
