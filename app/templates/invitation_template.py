"""
Team invitation email template for FastAPI authentication system.
Compatible with warolabs.com template structure and branding.

Localized via the same stack as the POS receipt
(app.core.localization.get_translator + babel_locale) so the copy and
<html lang> follow the tenant/user locale instead of being hardcoded Spanish.
"""
from typing import Optional

from app.core.localization import babel_locale, get_translator, normalize_locale
from app.core.platform_legal import waro_platform_footer_text

# English msgids — translated in app/locales/{es,en}/LC_MESSAGES/messages.po.
_ROLE_MSGIDS = {
    "admin": "Administrator",
    "superuser": "Super User",
}

_MSGID_GREETING_NAME = "Hello {invitee_name}!"
_MSGID_GREETING_PLAIN = "Hello!"
_MSGID_INVITE_BODY = "<strong>{inviter_name}</strong> has invited you to join the <strong>{tenant_name}</strong> team as <strong>{role}</strong>."
_MSGID_CTA_TEXT = "Click the link below to accept the invitation and get started:"
_MSGID_CTA_BUTTON = "Accept invitation"
_MSGID_VALIDITY = "This link is valid for 7 days."
_MSGID_IGNORE = "If you were not expecting this invitation, you can safely ignore this email."
_MSGID_SALUTATION = "Greetings from {tenant_name}'s ship."
_MSGID_TEAM_SIGNATURE = "{tenant_name} team"
_MSGID_SUBJECT = "You have been invited to join {brand_name}"


def _tr(_, msgid: str, **kwargs) -> str:
    text = _(msgid)
    return text.format(**kwargs) if kwargs else text


def _role_label(role_key: str, _) -> str:
    msgid = _ROLE_MSGIDS.get(role_key)
    return _(msgid) if msgid else role_key


def get_invitation_template(
    invitation_url: str,
    context: dict,
    locale: Optional[str] = None,
) -> str:
    """
    Generate team invitation email template with dynamic tenant branding and locale.

    Args:
        invitation_url: The complete invitation URL for accepting
        context: Template context with branding and invitation information.
                 ``role`` may be either a localization key (e.g. ``admin``) or a
                 ready-to-display string; keys in ``_ROLE_MSGIDS`` are localized.
        locale: Tenant/user locale (es|en); defaults to Spanish via normalize_locale

    Returns:
        HTML email template string
    """
    lang = normalize_locale(locale)
    _ = get_translator(lang)
    html_lang = babel_locale(lang)

    brand_name = context.get("brand_name", "Waro Colombia")
    tenant_name = context.get("tenant_name", "Waro Colombia")
    inviter_name = context.get("inviter_name", "")
    invitee_name = context.get("invitee_name", "")
    role_key = context.get("role", "")

    role = _role_label(role_key, _)
    greeting = _tr(_, _MSGID_GREETING_NAME, invitee_name=invitee_name) if invitee_name else _(_MSGID_GREETING_PLAIN)
    inviter = inviter_name or _("(admin)")
    body = _tr(_, _MSGID_INVITE_BODY, inviter_name=inviter, tenant_name=tenant_name, role=role)
    cta_text = _(_MSGID_CTA_TEXT)
    cta_button = _(_MSGID_CTA_BUTTON)
    validity = _(_MSGID_VALIDITY)
    ignore_note = _(_MSGID_IGNORE)
    salutation = _tr(_, _MSGID_SALUTATION, tenant_name=tenant_name)
    team_sig = _tr(_, _MSGID_TEAM_SIGNATURE, tenant_name=tenant_name)
    footer = waro_platform_footer_text(locale=lang)

    return f"""
<!DOCTYPE html>
<html lang="{html_lang}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_tr(_, _MSGID_SUBJECT, brand_name=brand_name)}</title>
</head>
<body style="font-family: Arial, sans-serif; color: black; margin: 0; padding: 0; text-align: left;">
    <div style="font-family: Arial, sans-serif; max-width: 600px; padding: 20px; border: 1px solid #ddd; border-radius: 8px;">
        <p>{greeting}</p>

        <p>{body}</p>

        <p>{cta_text}</p>

        <p style="text-align: center; margin: 30px 0;">
            <a href="{invitation_url}" style="color: white; background-color: #2563eb; padding: 14px 28px; border-radius: 6px; text-decoration: none; display: inline-block; font-weight: bold;">
                {cta_button}
            </a>
        </p>

        <p style="color: #666; font-size: 14px;">{validity}</p>

        <p>{ignore_note}</p>

        <p>{salutation}</p>

        <br><br>
        ----<br>
        {team_sig}<br>
        {footer}
    </div>
</body>
</html>
    """.strip()


def get_invitation_subject(brand_name: str, locale: Optional[str] = None) -> str:
    """Generate email subject line for team invitation, localized."""
    _ = get_translator(locale)
    return _tr(_, _MSGID_SUBJECT, brand_name=brand_name)
