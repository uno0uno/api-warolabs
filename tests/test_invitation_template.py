"""Tests for the localized team invitation email template (batch api-warolabs#929)."""
from app.templates.invitation_template import get_invitation_subject, get_invitation_template

_CONTEXT = {
    "brand_name": "WARO",
    "tenant_name": "WARO Colombia",
    "inviter_name": "Anderson",
    "invitee_name": "Maria",
    "role": "admin",
}


def test_invitation_subject_localizes_es_and_en():
    es = get_invitation_subject("WARO", locale="es")
    en = get_invitation_subject("WARO", locale="en")
    assert "Te han invitado a unirte a WARO" == es
    assert "You have been invited to join WARO" == en


def test_invitation_template_es_default():
    html = get_invitation_template("https://x.test/accept?token=1", _CONTEXT)
    assert 'lang="es_CO"' in html
    assert "¡Hola Maria!" in html
    assert "te ha invitado a unirte al equipo de" in html
    assert "<strong>WARO Colombia</strong>" in html
    assert "Administrador" in html  # role localized from "admin" key
    assert "Aceptar invitación" in html
    assert "válido por 7 días" in html


def test_invitation_template_en():
    html = get_invitation_template("https://x.test/accept?token=1", _CONTEXT, locale="en")
    assert 'lang="en_US"' in html
    assert "Hello Maria!" in html
    assert "has invited you to join the" in html
    assert "<strong>WARO Colombia</strong>" in html
    assert "Administrator" in html  # role localized from "admin" key
    assert "Accept invitation" in html
    assert "valid for 7 days" in html
    # No Spanish leaks into English
    assert "Aceptar invitación" not in html
    assert "Administrador" not in html


def test_invitation_template_superuser_role_localizes():
    ctx = {**_CONTEXT, "role": "superuser"}
    es = get_invitation_template("https://x.test/accept?token=1", ctx, locale="es")
    en = get_invitation_template("https://x.test/accept?token=1", ctx, locale="en")
    assert "Super Usuario" in es
    assert "Super User" in en


def test_invitation_template_falls_back_when_no_inviter_name():
    ctx = {**_CONTEXT, "inviter_name": ""}
    es = get_invitation_template("https://x.test/accept?token=1", ctx, locale="es")
    en = get_invitation_template("https://x.test/accept?token=1", ctx, locale="en")
    # Fallback "(admin)" placeholder renders in both locales
    assert "(admin)" in es
    assert "(admin)" in en
