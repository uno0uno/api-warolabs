"""
Email helper functions for sending formatted emails
"""
from typing import List, Dict, Any
from datetime import datetime
from pathlib import Path
from app.services.aws_ses_service import AWSSESService
from app.config import settings

async def send_quotation_email(
    supplier_email: str,
    supplier_name: str,
    purchase_number: str,
    purchase_date: datetime,
    delivery_date: datetime,
    items: List[Dict[str, Any]],
    notes: str = None,
    supplier_token: str = None,
    tenant_site: str = None
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

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        print(f"📧 Starting to send quotation email...")
        print(f"   To: {supplier_email}")
        print(f"   Supplier: {supplier_name}")
        print(f"   Quotation: {purchase_number}")

        # Format dates (handle None values)
        created_date = purchase_date.strftime('%d de %B de %Y') if purchase_date else 'Pendiente'
        required_date = delivery_date.strftime('%d de %B de %Y') if delivery_date else 'Por definir'

        # Build items list for text email
        items_list = "\n".join([
            f"{idx}. {item.get('ingredient_name', 'Producto')} - Cantidad: {item['quantity']} {item['unit']}"
            for idx, item in enumerate(items, 1)
        ])

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
            portal_link = f"\n\n🔗 Acceder a mi portal de proveedor:\n{base_url}/proveedor/{supplier_token}\n"

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
{items_list}{notes_text}{portal_link}

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
        print(f"📤 Preparing to send email via AWS SES...")
        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email="hola@warolabs.com",
            from_name="Waro Colombia",
            to_emails=[supplier_email],
            subject=f"Nueva Solicitud de Cotización - {purchase_number}",
            html_body=None,  # No HTML to avoid spam filters
            text_body=text_body
        )

        if success:
            print(f"✅ Email sent successfully to {supplier_email}")
        else:
            print(f"❌ Failed to send email to {supplier_email}")

        return success

    except Exception as e:
        print(f"❌ Error sending quotation email: {str(e)}")
        return False

async def send_purchase_status_notification(
    supplier_email: str,
    supplier_name: str,
    purchase_number: str,
    status: str,
    notes: str = None,
    metadata: dict = None,
    supplier_token: str = None,
    tenant_site: str = None
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
        print(f"📧 Sending status notification email...")
        print(f"   To: {supplier_email}")
        print(f"   Status: {status}")
        print(f"   Purchase: {purchase_number}")

        # Status titles and messages in Spanish
        status_info = {
            'confirmed': {
                'title': 'Orden Confirmada',
                'message': 'Tu cotización ha sido aprobada y confirmada. La orden de compra está lista para ser preparada.',
                'emoji': '✅'
            },
            'shipped': {
                'title': 'Orden Enviada por el Restaurante',
                'message': 'El restaurante ha marcado esta orden como enviada desde su ubicación.',
                'emoji': '🚚'
            },
            'received': {
                'title': 'Orden Recibida',
                'message': 'El restaurante ha confirmado la recepción de la orden.',
                'emoji': '📦'
            },
            'verified': {
                'title': 'Calidad Verificada',
                'message': 'El restaurante ha verificado la calidad de los productos recibidos.',
                'emoji': '✓'
            },
            'invoiced': {
                'title': 'Factura Registrada',
                'message': 'El restaurante ha registrado la factura de esta orden.',
                'emoji': '📄'
            },
            'paid': {
                'title': 'Pago Registrado',
                'message': 'El restaurante ha registrado el pago de esta orden. ¡Gracias por tu servicio!',
                'emoji': '💰'
            }
        }

        info = status_info.get(status, {
            'title': 'Actualización de Orden',
            'message': f'Tu orden ha sido actualizada al estado: {status}',
            'emoji': '📋'
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
            portal_link = f"\n\n🔗 Ver detalles en mi portal:\n{base_url}/proveedor/{supplier_token}\n"

        # Create text email
        text_body = f"""{info['emoji']} ¡Hola {supplier_name}!

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
        print(f"📤 Preparing to send notification email via AWS SES...")
        ses_service = AWSSESService()
        success = await ses_service.send_email(
            from_email="hola@warolabs.com",
            from_name="Waro Colombia",
            to_emails=[supplier_email],
            subject=f"{info['emoji']} {info['title']} - {purchase_number}",
            html_body=None,
            text_body=text_body
        )

        if success:
            print(f"✅ Status notification sent successfully to {supplier_email}")
        else:
            print(f"❌ Failed to send status notification to {supplier_email}")

        return success

    except Exception as e:
        print(f"❌ Error sending status notification: {str(e)}")
        return False
