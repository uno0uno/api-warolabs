"""
Billing Email Service — grace period reminders (#62) + payment events (#63)

Sends HTML reminder emails via AWS SES:
- Grace period reminders (days 1, 3, 6, 7 past_due) — triggered by cron
- Payment rejected / subscription paused — triggered by MP webhook
- Payment approved / period renewed — triggered by MP webhook
"""
import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.aws_ses_service import AWSSESService
from app.services.email_sender import resolve_sender_email_value
from app.services.billing_service import (
    GRACE_PERIOD_DAYS,
    get_past_due_tenants,
    get_trial_warning_candidates,
    record_reminder_sent,
    record_trial_warning_sent,
    reminder_already_sent,
    trial_warning_already_sent,
)

logger = logging.getLogger(__name__)

_ses = AWSSESService()

# Day buckets that trigger a reminder email
REMINDER_BUCKETS = [1, 3, 6, 7]

BILLING_URL = f"{settings.frontend_url}/billing"


def _render_reminder_html(
    tenant_name: str,
    days_overdue: int,
    grace_days_remaining: int,
    billing_url: str,
) -> str:
    """Render the grace period reminder email as HTML."""
    if days_overdue <= 3:
        urgency = "Aviso de pago"
        color = "#E87020"
        intro = (
            f"Hola {tenant_name}, hubo un problema al procesar tu pago de WARO. "
            f"Todavía tienes acceso completo, pero debes actualizar tu método de "
            f"pago en los próximos <strong>{grace_days_remaining} días</strong>."
        )
    elif days_overdue <= 6:
        urgency = "Acceso limitado — Renueva hoy"
        color = "#DC2626"
        intro = (
            f"Hola {tenant_name}, tu acceso a las funciones IA de WARO está suspendido "
            f"por falta de pago. Te quedan <strong>{grace_days_remaining} días</strong> "
            f"antes del bloqueo total. Renueva ahora para recuperar el acceso completo."
        )
    else:
        urgency = "Último aviso — Tu cuenta será bloqueada"
        color = "#991B1B"
        intro = (
            f"Hola {tenant_name}, mañana tu cuenta de WARO será bloqueada por falta de pago. "
            f"Para conservar tus datos y acceso, renueva tu suscripción ahora."
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{urgency}</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,0.08);">
        <!-- Header -->
        <tr>
          <td style="background:{color};padding:24px 32px;">
            <p style="margin:0;color:#ffffff;font-size:22px;font-weight:bold;">
              WARO — {urgency}
            </p>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:32px;">
            <p style="margin:0 0 16px;color:#1f2937;font-size:16px;line-height:1.6;">
              {intro}
            </p>
            <p style="margin:0 0 24px;color:#6b7280;font-size:14px;">
              Días vencido: <strong>{days_overdue}</strong> &nbsp;|&nbsp;
              Período de gracia restante: <strong>{grace_days_remaining} de {GRACE_PERIOD_DAYS} días</strong>
            </p>
            <a href="{billing_url}"
               style="display:inline-block;background:{color};color:#ffffff;
                      text-decoration:none;padding:14px 28px;border-radius:6px;
                      font-size:16px;font-weight:bold;">
              Renovar suscripción
            </a>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:16px 32px;border-top:1px solid #e5e7eb;">
            <p style="margin:0;color:#9ca3af;font-size:12px;">
              WARO Colombia &bull; Si ya realizaste el pago, ignora este mensaje.
              Puede tardar hasta 24 horas en verse reflejado.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _reminder_subject(days_overdue: int) -> str:
    if days_overdue <= 3:
        return "WARO — Actualiza tu método de pago"
    elif days_overdue <= 6:
        return "WARO — Tu acceso IA está suspendido, renueva hoy"
    return "WARO — Último aviso: tu cuenta será bloqueada mañana"


async def send_grace_reminder(tenant: Dict[str, Any]) -> bool:
    """
    Send a single grace reminder email to one tenant.

    Returns True if sent, False if skipped (no email, no SES config) or failed.
    """
    email = tenant.get("tenant_email")
    if not email:
        logger.info(
            "grace_reminder: tenant %s has no email — skipped",
            tenant["tenant_id"],
        )
        return False

    days_overdue = tenant["days_overdue"]
    grace_remaining = tenant["grace_days_remaining"]
    tenant_name = tenant.get("tenant_name") or "Cliente"

    subject = _reminder_subject(days_overdue)
    html = _render_reminder_html(
        tenant_name=tenant_name,
        days_overdue=days_overdue,
        grace_days_remaining=grace_remaining,
        billing_url=BILLING_URL,
    )

    sent = await _ses.send_email(
        from_email=resolve_sender_email_value(email),
        from_name="WARO Colombia",
        to_emails=[email],
        subject=subject,
        html_body=html,
    )

    if sent:
        logger.info(
            "grace_reminder: sent day-%d email to tenant=%s email=%s",
            days_overdue,
            tenant["tenant_id"],
            email,
        )
    else:
        logger.warning(
            "grace_reminder: SES returned False for tenant=%s",
            tenant["tenant_id"],
        )

    return sent


async def process_grace_reminders(conn) -> Dict[str, Any]:
    """
    Main entry point for the cron job.

    1. Fetches all past_due tenants from DB
    2. For each tenant in a reminder bucket (days 1, 3, 6, 7):
       - Checks deduplication via billing_events
       - Sends email via AWS SES
       - Records send event to prevent duplicates
    3. Returns a summary dict with sent/skipped/error counts.
    """
    tenants = await get_past_due_tenants(conn)

    sent_count = 0
    skipped_count = 0
    error_count = 0
    details: List[Dict[str, Any]] = []

    for tenant in tenants:
        days = tenant["days_overdue"]

        # Only send on bucket days (1, 3, 6, 7) — skip other days
        if days not in REMINDER_BUCKETS:
            skipped_count += 1
            continue

        sub_id = tenant["subscription_id"]

        already_sent = await reminder_already_sent(conn, sub_id, days)
        if already_sent:
            skipped_count += 1
            details.append({
                "tenant_id": tenant["tenant_id"],
                "days_overdue": days,
                "result": "already_sent",
            })
            continue

        success = await send_grace_reminder(tenant)

        if success:
            await record_reminder_sent(conn, tenant["tenant_id"], sub_id, days)
            sent_count += 1
            details.append({
                "tenant_id": tenant["tenant_id"],
                "days_overdue": days,
                "result": "sent",
            })
        else:
            error_count += 1
            details.append({
                "tenant_id": tenant["tenant_id"],
                "days_overdue": days,
                "result": "error",
            })

    logger.info(
        "process_grace_reminders: sent=%d skipped=%d error=%d total=%d",
        sent_count, skipped_count, error_count, len(tenants),
    )

    return {
        "total_past_due": len(tenants),
        "sent": sent_count,
        "skipped": skipped_count,
        "error": error_count,
        "details": details,
    }


def _trial_warning_subject(days_remaining: int) -> str:
    if days_remaining == 1:
        return "WARO — Tu prueba vence mañana"
    return f"WARO — Tu prueba vence en {days_remaining} días"


async def send_trial_warning(trial: Dict[str, Any]) -> bool:
    """Send a trial reminder without persisting or logging recipient PII."""
    email = trial.get("tenant_email")
    if not email:
        logger.info("trial_warning: tenant=%s has no email", trial["tenant_id"])
        return False

    days_remaining = trial["days_remaining"]
    tenant_name = trial.get("tenant_name") or "Cliente"
    trial_end = trial["trial_ends_at"].date().isoformat()
    text = (
        f"Hola {tenant_name},\n\n"
        f"Tu prueba de WARO vence en {days_remaining} día(s), el {trial_end}.\n"
        "El aviso no cambia tu acceso. Puedes activar tu suscripción desde:\n"
        f"{BILLING_URL}\n\n"
        "Si ya realizaste el pago, puedes ignorar este mensaje."
    )
    sent = await _ses.send_email(
        from_email=resolve_sender_email_value(),
        from_name="WARO Colombia",
        to_emails=[email],
        subject=_trial_warning_subject(days_remaining),
        text_body=text,
    )
    if sent:
        logger.info(
            "trial_warning: sent day-%d tenant=%s",
            days_remaining,
            trial["tenant_id"],
        )
    else:
        logger.warning("trial_warning: SES failed tenant=%s", trial["tenant_id"])
    return sent


async def process_trial_warnings(conn) -> Dict[str, int]:
    """Send and durably deduplicate 7/3/1-day trial warnings."""
    candidates = await get_trial_warning_candidates(conn)
    sent = 0
    skipped = 0
    error = 0

    for trial in candidates:
        already_sent = await trial_warning_already_sent(
            conn,
            trial["subscription_id"],
            trial["days_remaining"],
        )
        if already_sent:
            skipped += 1
            continue
        if not await send_trial_warning(trial):
            error += 1
            continue
        await record_trial_warning_sent(
            conn,
            tenant_id=trial["tenant_id"],
            subscription_id=trial["subscription_id"],
            days_remaining=trial["days_remaining"],
            trial_ends_at=trial["trial_ends_at"],
        )
        sent += 1

    logger.info(
        "process_trial_warnings: sent=%d skipped=%d error=%d total=%d",
        sent,
        skipped,
        error,
        len(candidates),
    )
    return {"sent": sent, "skipped": skipped, "error": error}


# ── Payment event emails — issue #63 ─────────────────────────────────────────


async def send_payment_rejected_email(
    tenant_name: str,
    tenant_email: Optional[str],
    billing_url: str,
) -> bool:
    """
    Send a payment rejection email when MP reports payment → rejected
    or subscription_preapproval → paused.

    Returns False if tenant has no email or SES fails.
    """
    if not tenant_email:
        logger.info("send_payment_rejected_email: no email for tenant %s — skipped", tenant_name)
        return False

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pago rechazado — WARO</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:#DC2626;padding:24px 32px;">
            <p style="margin:0;color:#ffffff;font-size:22px;font-weight:bold;">
              WARO — Tu pago fue rechazado
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:32px;">
            <p style="margin:0 0 16px;color:#1f2937;font-size:16px;line-height:1.6;">
              Hola {tenant_name}, no pudimos procesar tu pago de WARO.
              Tu acceso puede verse limitado si no actualizas tu método de pago pronto.
            </p>
            <p style="margin:0 0 24px;color:#6b7280;font-size:14px;">
              Esto puede ocurrir si tu tarjeta venció, no tenía fondos suficientes,
              o tu banco rechazó el cargo. Actualiza tu método de pago para continuar
              usando WARO sin interrupciones.
            </p>
            <a href="{billing_url}"
               style="display:inline-block;background:#DC2626;color:#ffffff;
                      text-decoration:none;padding:14px 28px;border-radius:6px;
                      font-size:16px;font-weight:bold;">
              Actualizar método de pago
            </a>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px;border-top:1px solid #e5e7eb;">
            <p style="margin:0;color:#9ca3af;font-size:12px;">
              WARO Colombia &bull; Si ya actualizaste tu pago, ignora este mensaje.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    sent = await _ses.send_email(
        from_email=resolve_sender_email_value(tenant_email),
        from_name="WARO Colombia",
        to_emails=[tenant_email],
        subject="WARO — Tu pago fue rechazado, actualiza tu método de pago",
        html_body=html,
    )

    if sent:
        logger.info("send_payment_rejected_email: sent to %s", tenant_email)
    else:
        logger.warning("send_payment_rejected_email: SES failed for %s", tenant_email)

    return sent


async def send_payment_renewed_email(
    tenant_name: str,
    tenant_email: Optional[str],
    next_period_end: str,
) -> bool:
    """
    Send a subscription renewal confirmation email when MP reports payment → approved.

    Returns False if tenant has no email or SES fails.
    """
    if not tenant_email:
        logger.info("send_payment_renewed_email: no email for tenant %s — skipped", tenant_name)
        return False

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Suscripción renovada — WARO</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:#16A34A;padding:24px 32px;">
            <p style="margin:0;color:#ffffff;font-size:22px;font-weight:bold;">
              WARO — Tu suscripción fue renovada
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:32px;">
            <p style="margin:0 0 16px;color:#1f2937;font-size:16px;line-height:1.6;">
              Hola {tenant_name}, tu suscripción a WARO ha sido renovada exitosamente.
              Tienes acceso completo hasta el <strong>{next_period_end}</strong>.
            </p>
            <p style="margin:0 0 24px;color:#6b7280;font-size:14px;">
              Gracias por seguir confiando en WARO para gestionar tu restaurante.
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px;border-top:1px solid #e5e7eb;">
            <p style="margin:0;color:#9ca3af;font-size:12px;">
              WARO Colombia &bull; Este es un correo de confirmación automático.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    sent = await _ses.send_email(
        from_email=resolve_sender_email_value(tenant_email),
        from_name="WARO Colombia",
        to_emails=[tenant_email],
        subject="WARO — Tu suscripción fue renovada exitosamente",
        html_body=html,
    )

    if sent:
        logger.info("send_payment_renewed_email: sent to %s", tenant_email)
    else:
        logger.warning("send_payment_renewed_email: SES failed for %s", tenant_email)

    return sent
