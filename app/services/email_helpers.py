"""
Email helper functions for sending formatted emails
"""
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.config import settings
from app.services.aws_ses_service import AWSSESService
from app.database import get_db_connection
from app.services.email_sender import resolve_sender_email_for_tenant
from app.services.aws_s3_service import AWSS3Service
from app.core.localization import (
    TenantLocaleSettings,
    normalize_currency,
    normalize_locale,
    resolve_tenant_locale_settings,
)
from app.core.timezones import normalize_timezone
from app.templates.order_confirmation_template import (
    get_order_confirmation_text,
    get_order_confirmation_subject,
    get_order_accepted_text,
    get_order_accepted_subject,
)
from app.templates.pos_receipt_template import (
    get_pos_receipt_text,
    get_pos_receipt_subject,
)
from app.templates.negocio_welcome_template import (
    get_negocio_welcome_text,
    get_negocio_welcome_subject,
)
from app.templates.credit_abono_receipt_template import (
    get_credit_abono_subject,
    get_credit_abono_text,
)
from app.templates.wallet_recharge_receipt_template import (
    get_wallet_recharge_subject,
    get_wallet_recharge_text,
)
import logging

logger = logging.getLogger(__name__)

async def send_quotation_email(
    supplier_email: str,
    supplier_name: str,
    purchase_number: str,
    purchase_date: datetime,
    delivery_date: datetime,
    items: List[Dict[str, Any]],
    notes: str = None,
    supplier_token: str = None,
    tenant_site: str = None,
    payment_type: str = None,
    payment_terms: str = None,
    credit_days: int = None,
    requires_advance_payment: bool = False,
    consolidation_group: str = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """
    Send a quotation request email to a supplier

    Args:
        supplier_email: Supplier's email address
        supplier_name: Supplier's name
        purchase_number: Generated purchase/quotation number (e.g., WR-2025-0001)
        purchase_date: Date the quotation was created
        delivery_date: Required delivery date
        items: List of items with ingredient_name, quantity, unit
        notes: Optional notes for the supplier
        supplier_token: Supplier's access token for portal link
        tenant_site: Tenant's site domain (e.g., 'warocol.com')
        payment_type: Type of payment (contado, credito, contraentrega)
        payment_terms: Payment terms text
        credit_days: Credit days for deferred payments
        requires_advance_payment: Whether advance payment is required

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:

        # Format dates (handle None values)
        created_date = purchase_date.strftime('%d de %B de %Y') if purchase_date else 'Pendiente'
        required_date = delivery_date.strftime('%d de %B de %Y') if delivery_date else 'Por definir'

        # Build items list for text email
        items_list = "\n".join([
            f"{idx}. {item.get('ingredient_name', 'Producto')} - Cantidad: {item['quantity']} {item['unit']}"
            for idx, item in enumerate(items, 1)
        ])

        # Build payment information section
        payment_info_text = ""
        if payment_type:
            payment_type_names = {
                'contado': 'Contado - Pago Inmediato',
                'credito': 'Crédito - Pago Diferido',
                'contraentrega': 'Contraentrega - Pago al Recibir'
            }
            payment_type_display = payment_type_names.get(payment_type, payment_type)

            payment_info_text = f"\n\nCONDICIONES DE PAGO\n--------------------\nTipo de Pago: {payment_type_display}\n"

            if credit_days:
                payment_info_text += f"Plazo de Crédito: {credit_days} días\n"

            if payment_terms:
                payment_info_text += f"Términos: {payment_terms}\n"

            if consolidation_group:
                payment_info_text += f"Grupo de Consolidación: {consolidation_group}\n"

            if requires_advance_payment:
                payment_info_text += "\n⚠️ IMPORTANTE: Esta orden requiere anticipo antes del envío\n"

        # Build notes section if exists
        notes_text = f"\n\nNotas:\n{notes}" if notes else ""

        # Build portal link if token is provided
        # Use same routing logic as MagicLink (lines 78-83 in magic_link_service.py)
        if settings.is_development:
            # In development, redirect to frontend (runs on port 8080)
            base_url = "http://localhost:8080"
        else:
            # In production, use the detected tenant site from database
            base_url = f"https://{tenant_site}" if tenant_site else "https://warocol.com"

        portal_link = ""
        if supplier_token:
            portal_link = f"\n\nAcceder a mi portal de proveedor:\n{base_url}/proveedor/{supplier_token}\n"

        # Create simple text email
        text_body = f"""¡Hola {supplier_name}!

Tienes una nueva solicitud de cotización de Waro Colombia.

RESUMEN DE LA COTIZACIÓN
------------------------
Número de Cotización: {purchase_number}
Fecha de Solicitud: {created_date}
Fecha Requerida de Entrega: {required_date}

PRODUCTOS SOLICITADOS
---------------------
{items_list}{payment_info_text}{notes_text}{portal_link}

Por favor, accede al portal para completar los precios de la cotización.

Si no solicitaste esta cotización, puedes ignorar este correo de forma segura.

Saludos desde la nave de Waro Colombia.

----
Saifer 101 (Anderson Arévalo)
Fundador Waro Colombia
Dirección: Calle 39F # 68F - 66 Sur
Bogotá, D.C, Colombia
Tel: 3142047013
Correo: anderson.arevalo@warocol.com
Tecnología colombiana para el mundo.
"""

        # Send email (text only, no HTML to avoid spam)

        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name="Saifer 101 de Waro Colombia",
            to_emails=[supplier_email],
            subject=f"Nueva Solicitud de Cotización - {purchase_number}",
            html_body=None,  # No HTML to avoid spam filters
            text_body=text_body
        )

        if success:
            pass
        else:
            pass

        return success

    except Exception:
        pass
        return False

async def send_purchase_status_notification(
    supplier_email: str,
    supplier_name: str,
    purchase_number: str,
    status: str,
    notes: str = None,
    metadata: dict = None,
    supplier_token: str = None,
    tenant_site: str = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """
    Send a status update notification email to a supplier

    Args:
        supplier_email: Supplier's email address
        supplier_name: Supplier's name
        purchase_number: Purchase number (e.g., WR-2025-0001)
        status: New status of the purchase
        notes: Optional notes about the status change
        metadata: Optional metadata with additional info (tracking, invoice, payment details)
        supplier_token: Supplier's access token for portal link
        tenant_site: Tenant's site domain (e.g., 'warocol.com')

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:

        # Status titles and messages in Spanish
        status_info = {
            'confirmed': {
                'title': 'Orden Confirmada',
                'message': 'Tu cotización ha sido aprobada y confirmada. La orden de compra está lista para ser preparada.'
            },
            'shipped': {
                'title': 'Orden Enviada por el Restaurante',
                'message': 'El restaurante ha marcado esta orden como enviada desde su ubicación.'
            },
            'received': {
                'title': 'Orden Recibida',
                'message': 'El restaurante ha confirmado la recepción de la orden.'
            },
            'verified': {
                'title': 'Calidad Verificada',
                'message': 'El restaurante ha verificado la calidad de los productos recibidos.'
            },
            'invoiced': {
                'title': 'Factura Registrada',
                'message': 'El restaurante ha registrado la factura de esta orden.'
            },
            'paid': {
                'title': 'Pago Registrado',
                'message': 'El restaurante ha registrado el pago de esta orden. ¡Gracias por tu servicio!'
            }
        }

        info = status_info.get(status, {
            'title': 'Actualización de Orden',
            'message': f'Tu orden ha sido actualizada al estado: {status}'
        })

        # Build metadata section if exists
        metadata_text = ""
        if metadata:
            metadata_text = "\n\nDETALLES ADICIONALES\n--------------------\n"
            if metadata.get('tracking_number'):
                metadata_text += f"Número de Rastreo: {metadata['tracking_number']}\n"
            if metadata.get('carrier'):
                metadata_text += f"Transportadora: {metadata['carrier']}\n"
            if metadata.get('estimated_delivery_date'):
                metadata_text += f"Fecha Estimada de Entrega: {metadata['estimated_delivery_date']}\n"
            if metadata.get('invoice_number'):
                metadata_text += f"Número de Factura: {metadata['invoice_number']}\n"
            if metadata.get('invoice_date'):
                metadata_text += f"Fecha de Factura: {metadata['invoice_date']}\n"
            if metadata.get('invoice_total'):
                metadata_text += f"Total de Factura: ${metadata['invoice_total']:,.2f}\n"
            if metadata.get('payment_method'):
                metadata_text += f"Método de Pago: {metadata['payment_method']}\n"
            if metadata.get('payment_reference'):
                metadata_text += f"Referencia de Pago: {metadata['payment_reference']}\n"
            if metadata.get('payment_date'):
                metadata_text += f"Fecha de Pago: {metadata['payment_date']}\n"

        # Build notes section if exists
        notes_text = f"\n\nNotas:\n{notes}" if notes else ""

        # Build portal link
        if settings.is_development:
            base_url = "http://localhost:8080"
        else:
            base_url = f"https://{tenant_site}" if tenant_site else "https://warocol.com"

        portal_link = ""
        if supplier_token:
            portal_link = f"\n\nVer detalles en mi portal:\n{base_url}/proveedor/{supplier_token}\n"

        # Create text email
        text_body = f"""¡Hola {supplier_name}!

Tu orden de compra ha sido actualizada.

{info['title'].upper()}
------------------------
Número de Orden: {purchase_number}
Estado: {info['title']}

{info['message']}{metadata_text}{notes_text}{portal_link}

Puedes acceder al portal para ver todos los detalles de tu orden.

Saludos desde la nave de Waro Colombia.

----
Saifer 101 (Anderson Arévalo)
Fundador Waro Colombia
Dirección: Calle 39F # 68F - 66 Sur
Bogotá, D.C, Colombia
Tel: 3142047013
Correo: anderson.arevalo@warocol.com
Tecnología colombiana para el mundo.
"""

        # Send email

        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name="Saifer 101 de Waro Colombia",
            to_emails=[supplier_email],
            subject=f"{purchase_number} - {info['title']}",
            html_body=None,
            text_body=text_body
        )

        if success:
            pass
        else:
            pass

        return success

    except Exception:
        pass
        return False


async def send_negocio_welcome_email(
    owner_email: str,
    display_name: str,
    tenant_id: Optional[str] = None,
) -> bool:
    """
    Send a welcome email when a negocio activates its public profile for the first time.

    Called fire-and-forget via asyncio.create_task — never raises.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    try:
        text_body = get_negocio_welcome_text(display_name)
        subject = get_negocio_welcome_subject(display_name)

        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name="Saifer 101 de WaRo Colombia",
            to_emails=[owner_email],
            subject=subject,
            html_body=None,
            text_body=text_body,
        )

        if success:
            logger.info(f"Negocio welcome email sent to {owner_email} for '{display_name}'")
        else:
            logger.warning(f"SES returned failure for negocio welcome email to {owner_email}")

        return success

    except Exception as e:
        logger.error(f"Error sending negocio welcome email to {owner_email}: {e}")
        return False


async def send_order_confirmation_email(
    customer_email: str,
    order_number: int,
    order_type: str,
    order_date: datetime,
    items: List[Dict[str, Any]],
    subtotal: float,
    delivery_address: Optional[Dict[str, Any]] = None,
    scheduled_time: Optional[datetime] = None,
    delivery_instructions: Optional[str] = None,
    pickup_pin: Optional[str] = None,
    order_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """
    Send a transactional order confirmation email to the customer.

    Called immediately after checkout_cart() commits the order.
    Never raises — logs on failure so the order is never rolled back.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    try:
        text_body = get_order_confirmation_text(
            order_number=order_number,
            order_type=order_type,
            order_date=order_date,
            items=items,
            subtotal=subtotal,
            delivery_address=delivery_address,
            scheduled_time=scheduled_time,
            delivery_instructions=delivery_instructions,
            pickup_pin=pickup_pin,
            order_id=order_id,
        )
        subject = get_order_confirmation_subject(order_number)

        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name="WARO Colombia",
            to_emails=[customer_email],
            subject=subject,
            html_body=None,
            text_body=text_body,
        )

        if success:
            logger.info(f"Order confirmation email sent for order #{order_number} to {customer_email}")
        else:
            logger.warning(f"SES returned failure for order confirmation email #{order_number}")

        return success

    except Exception as e:
        logger.error(f"Error sending order confirmation email for order #{order_number}: {e}")
        return False


async def send_order_accepted_email(
    customer_email: str,
    order_number: int,
    order_type: str,
    items: List[Dict[str, Any]],
    subtotal: float,
    delivery_address: Optional[Dict[str, Any]] = None,
    order_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """
    Send an order acceptance email to the customer.

    Called fire-and-forget (via asyncio.create_task) when a restaurant accepts
    an order via the auto_complete path (pending → completed).
    Never raises — logs on failure so the PATCH response is never delayed.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    try:
        text_body = get_order_accepted_text(
            order_number=order_number,
            order_type=order_type,
            items=items,
            subtotal=subtotal,
            delivery_address=delivery_address,
            order_id=order_id,
        )
        subject = get_order_accepted_subject(order_number)

        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name="WARO Colombia",
            to_emails=[customer_email],
            subject=subject,
            html_body=None,
            text_body=text_body,
        )

        if success:
            logger.info(f"Order accepted email sent for order #{order_number} to {customer_email}")
        else:
            logger.warning(f"SES returned failure for order accepted email #{order_number}")

        return success

    except Exception as e:
        logger.error(f"Error sending order accepted email for order #{order_number}: {e}")
        return False


def _build_receipt_html_with_pixel(
    text_body: str,
    tracking_pixel_url: Optional[str],
) -> Optional[str]:
    """Wrap the plain-text receipt in a minimal HTML body with a 1x1 pixel.

    Returns None when no pixel is requested, so existing flows (POS receipts
    without tracking) keep sending text-only emails untouched.

    The HTML is a faithful <pre> render of the text body — no extra content,
    no claims of "read", just the open-detection signal (api-warolabs#657).
    """
    if not tracking_pixel_url:
        return None
    from html import escape

    escaped_text = escape(text_body or "")
    escaped_pixel = escape(tracking_pixel_url, quote=True)
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;">'
        f'<pre style="font-family:monospace;font-size:13px;white-space:pre-wrap;'
        f'word-wrap:break-word;margin:0;padding:16px;">{escaped_text}</pre>'
        f'<img src="{escaped_pixel}" width="1" height="1" alt="" '
        'style="display:block;width:1px;height:1px;border:0;" />'
        "</body></html>"
    )


async def send_pos_receipt_email(
    customer_email: str,
    order_number: int,
    total_amount: float,
    payment_method: str,
    items: List[Dict[str, Any]],
    order_date: datetime,
    tenant_id: Optional[str] = None,
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
    return_details: bool = False,
    tracking_pixel_url: Optional[str] = None,
) -> Any:
    """
    Send a POS receipt email to the customer after a point-of-sale order completes.

    Called fire-and-forget from complete_pos_order() — never raises, never blocks the order.

    Returns:
        True if sent successfully, False otherwise.
    """
    # Defense in depth (issue #134): the API schema (SendReceiptRequest /
    # CompleteOrderRequest) already validates with Pydantic EmailStr, but
    # any future internal caller (cron, ad-hoc script) could bypass that.
    # A truthy string with no '@' would crash AWS SES with InvalidParameterValue.
    if not customer_email or '@' not in customer_email:
        logger.warning(
            f"Receipt email skipped: invalid customer_email={customer_email!r} for order #{order_number}"
        )
        if return_details:
            return {
                "success": False,
                "attachments": {"pdf": False, "xml": False},
                "attachment_warnings": ["invalid_email"],
            }
        return False

    locale_settings = TenantLocaleSettings(
        locale=normalize_locale(locale),
        currency_code=normalize_currency(currency_code),
        timezone=normalize_timezone(timezone),
    )
    resolved_tip_label = tip_label.strip()[:40] if tip_label else None
    if tenant_id:
        try:
            async with get_db_connection(use_transaction=False) as conn:
                locale_settings = await resolve_tenant_locale_settings(conn, tenant_id)
                tip_row = await conn.fetchrow(
                    "SELECT receipt_tip_label FROM tenant_fiscal_data WHERE tenant_id = $1",
                    tenant_id,
                )
                if tip_label is None and tip_row and tip_row.get("receipt_tip_label"):
                    resolved_tip_label = str(tip_row["receipt_tip_label"]).strip()[:40] or None
        except Exception as _settings_err:
            logger.warning(f"Could not fetch receipt locale/tip settings for tenant {tenant_id}: {_settings_err}")

    try:
        ses_service = AWSSESService()
        attachments: List[dict] = []
        attachment_status = {"pdf": False, "xml": False}
        attachment_warnings: List[str] = []

        # Attach DIAN document (PDF/XML) when we have invoice identifiers and tenant context.
        # Tenant context is required to avoid leaking invoice files.
        if tenant_id and invoice_prefix and invoice_number:
            try:
                async with get_db_connection(use_transaction=False) as conn:
                    inv_row = await conn.fetchrow(
                        """
                        SELECT id, r2_pdf_key, r2_xml_key, prefix, invoice_number
                        FROM electronic_invoices
                        WHERE tenant_id = $1
                          AND prefix = $2
                          AND invoice_number = $3
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        tenant_id,
                        invoice_prefix,
                        int(invoice_number),
                    )

                if inv_row:
                    track_id = str(inv_row["id"])
                    s3 = AWSS3Service()

                    if inv_row.get("r2_pdf_key"):
                        pdf_bytes = await s3.get_object_bytes(inv_row["r2_pdf_key"])
                        if pdf_bytes:
                            attachment_status["pdf"] = True
                            attachments.append({
                                "data": pdf_bytes,
                                "filename": f"{invoice_prefix}-{invoice_number}.pdf",
                                "content_type": "application/pdf",
                            })
                        else:
                            attachment_warnings.append("invoice_pdf_unavailable")
                    else:
                        attachment_warnings.append("invoice_pdf_missing")

                    if inv_row.get("r2_xml_key"):
                        xml_bytes = await s3.get_object_bytes(inv_row["r2_xml_key"])
                        if xml_bytes:
                            attachment_status["xml"] = True
                            attachments.append({
                                "data": xml_bytes,
                                "filename": f"{invoice_prefix}-{invoice_number}.xml",
                                "content_type": "application/xml",
                            })
                        else:
                            attachment_warnings.append("invoice_xml_unavailable")
                    else:
                        attachment_warnings.append("invoice_xml_missing")

                    if not attachments:
                        logger.info(
                            f"Receipt email: invoice exists but has no files yet (track_id={track_id})"
                        )
                else:
                    attachment_warnings.append("invoice_record_not_found")
            except Exception as _att_err:
                attachment_warnings.append("invoice_attachments_unavailable")
                logger.warning(f"Could not attach invoice files to receipt email: {_att_err}")

        presentation = dict(invoice_presentation or {})
        if invoice_prefix and invoice_number:
            presentation["attachments"] = attachment_status

        text_body = get_pos_receipt_text(
            order_number=order_number,
            total_amount=total_amount,
            payment_method=payment_method,
            items=items,
            order_date=order_date,
            business_name=business_name,
            business_address=business_address,
            business_city=business_city,
            business_phone=business_phone,
            discount_amount=discount_amount,
            subtotal=subtotal,
            standard_tax=standard_tax,
            liquor_tax=liquor_tax,
            standard_tax_label=standard_tax_label,
            invoice_prefix=invoice_prefix,
            invoice_number=invoice_number,
            invoice_cufe=invoice_cufe,
            invoice_presentation=presentation,
            tip_amount=tip_amount,
            tip_label=resolved_tip_label,
            promo_savings=promo_savings,
            promo_breakdown=promo_breakdown,
            waro_redemption_summary=waro_redemption_summary,
            locale=locale_settings.locale,
            currency_code=locale_settings.currency_code,
            timezone=locale_settings.timezone,
        )
        subject = get_pos_receipt_subject(
            order_number,
            business_name=business_name,
            invoice_prefix=invoice_prefix,
            invoice_number=invoice_number,
            locale=locale_settings.locale,
        )

        # api-warolabs#657: when a tracking pixel is provided, wrap the text
        # receipt in a minimal HTML body (text stays as multipart alternative).
        html_body = _build_receipt_html_with_pixel(text_body, tracking_pixel_url)

        if attachments:
            success = await ses_service.send_email_with_attachments(
                from_email=await resolve_sender_email_for_tenant(tenant_id),
                from_name=business_name or "WARO Colombia",
                to_emails=[customer_email],
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                attachments=attachments,
            )
        else:
            success = await ses_service.send_email(
                from_email=await resolve_sender_email_for_tenant(tenant_id),
                from_name=business_name or "WARO Colombia",
                to_emails=[customer_email],
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )

        if success:
            logger.info(f"POS receipt email sent for order #{order_number} to {customer_email}")
        else:
            logger.warning(f"SES returned failure for POS receipt email #{order_number}")

        if return_details:
            return {
                "success": success,
                "attachments": attachment_status,
                "attachment_warnings": attachment_warnings,
            }
        return success

    except Exception as e:
        logger.error(f"Error sending POS receipt email for order #{order_number}: {e}")
        if return_details:
            return {
                "success": False,
                "attachments": {"pdf": False, "xml": False},
                "attachment_warnings": ["email_send_error"],
            }
        return False


async def send_credit_abono_receipt_email(
    customer_email: str,
    customer_name: str,
    payment_date_label: str,
    payment_method_label: str,
    total_amount: float,
    lines: List[Dict[str, Any]],
    notes: Optional[str] = None,
    total_outstanding_after: Optional[float] = None,
    tenant_id: Optional[str] = None,
    business_name: Optional[str] = None,
    business_address: Optional[str] = None,
    business_city: Optional[str] = None,
    business_phone: Optional[str] = None,
) -> bool:
    """
    Send a credit cartera abono receipt email (CRM / Finanzas).
    Never raises — returns False on validation or SES failure.
    """
    if not customer_email or "@" not in customer_email:
        logger.warning(
            "Credit abono receipt email skipped: invalid customer_email=%r",
            customer_email,
        )
        return False

    locale_settings = TenantLocaleSettings(
        locale=normalize_locale(None),
        currency_code=normalize_currency(None),
        timezone=normalize_timezone(None),
    )
    if tenant_id:
        try:
            async with get_db_connection(use_transaction=False) as conn:
                locale_settings = await resolve_tenant_locale_settings(conn, tenant_id)
        except Exception as exc:
            logger.warning("Credit abono receipt locale resolve failed: %s", exc)

    locale = locale_settings.locale
    currency_code = locale_settings.currency_code

    text_body = get_credit_abono_text(
        customer_name=customer_name,
        payment_date_label=payment_date_label,
        payment_method_label=payment_method_label,
        total_amount=total_amount,
        lines=lines,
        notes=notes,
        total_outstanding_after=total_outstanding_after,
        business_name=business_name,
        business_address=business_address,
        business_city=business_city,
        business_phone=business_phone,
        locale=locale,
        currency_code=currency_code,
    )
    subject = get_credit_abono_subject(business_name, locale=locale)
    escaped_text = text_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body = (
        f'<html><body style="font-family:Arial,sans-serif;color:#111;">'
        f'<pre style="font-family:monospace;font-size:13px;white-space:pre-wrap;'
        f'word-wrap:break-word;margin:0;padding:16px;">{escaped_text}</pre>'
        "</body></html>"
    )

    try:
        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name=business_name or "WARO Colombia",
            to_emails=[customer_email],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        if success:
            logger.info("Credit abono receipt email sent to %s", customer_email)
        else:
            logger.warning("SES returned failure for credit abono receipt to %s", customer_email)
        return success
    except Exception as exc:
        logger.error("Error sending credit abono receipt email: %s", exc)
        return False


async def send_wallet_recharge_receipt_email(
    customer_email: str,
    customer_name: str,
    recharge_date_label: str,
    payment_method_label: str,
    amount_cop: float,
    balance_after_cop: float,
    notes: Optional[str] = None,
    tenant_id: Optional[str] = None,
    business_name: Optional[str] = None,
    business_address: Optional[str] = None,
    business_city: Optional[str] = None,
    business_phone: Optional[str] = None,
) -> bool:
    """
    Send a wallet recharge receipt email (CRM client detail).
    Never raises — returns False on validation or SES failure.
    """
    if not customer_email or "@" not in customer_email:
        logger.warning(
            "Wallet recharge receipt email skipped: invalid customer_email=%r",
            customer_email,
        )
        return False

    locale_settings = TenantLocaleSettings(
        locale=normalize_locale(None),
        currency_code=normalize_currency(None),
        timezone=normalize_timezone(None),
    )
    if tenant_id:
        try:
            async with get_db_connection(use_transaction=False) as conn:
                locale_settings = await resolve_tenant_locale_settings(conn, tenant_id)
        except Exception as exc:
            logger.warning("Wallet recharge receipt locale resolve failed: %s", exc)

    locale = locale_settings.locale
    currency_code = locale_settings.currency_code

    text_body = get_wallet_recharge_text(
        customer_name=customer_name,
        recharge_date_label=recharge_date_label,
        payment_method_label=payment_method_label,
        amount_cop=amount_cop,
        balance_after_cop=balance_after_cop,
        notes=notes,
        business_name=business_name,
        business_address=business_address,
        business_city=business_city,
        business_phone=business_phone,
        locale=locale,
        currency_code=currency_code,
    )
    subject = get_wallet_recharge_subject(business_name, locale=locale)
    escaped_text = text_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body = (
        f'<html><body style="font-family:Arial,sans-serif;color:#111;">'
        f'<pre style="font-family:monospace;font-size:13px;white-space:pre-wrap;'
        f'word-wrap:break-word;margin:0;padding:16px;">{escaped_text}</pre>'
        "</body></html>"
    )

    try:
        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email=await resolve_sender_email_for_tenant(tenant_id),
            from_name=business_name or "WARO Colombia",
            to_emails=[customer_email],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        if success:
            logger.info("Wallet recharge receipt email sent to %s", customer_email)
        else:
            logger.warning("SES returned failure for wallet recharge receipt to %s", customer_email)
        return success
    except Exception as exc:
        logger.error("Error sending wallet recharge receipt email: %s", exc)
        return False
