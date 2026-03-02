"""
Discord webhook notification service
Sends notifications to Discord channels via webhooks
"""
import logging
import httpx
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class DiscordWebhookService:
    """Service for sending notifications to Discord via webhooks"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def send_notification(
        self,
        title: str,
        description: str,
        color: int = 3447003,  # Blue color
        fields: Optional[list[Dict[str, Any]]] = None,
        footer: Optional[str] = None
    ) -> bool:
        """
        Send a notification to Discord webhook

        Args:
            title: Title of the embed
            description: Description text
            color: Color of the embed (decimal)
            fields: List of fields with name, value, inline
            footer: Footer text

        Returns:
            bool: True if notification sent successfully, False otherwise
        """
        try:
            # Build Discord embed
            embed = {
                "title": title,
                "description": description,
                "color": color,
                "timestamp": None  # Discord will use current time
            }

            if fields:
                embed["fields"] = fields

            if footer:
                embed["footer"] = {"text": footer}

            payload = {
                "embeds": [embed]
            }

            # Send webhook request
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.webhook_url,
                    json=payload
                )

                if response.status_code == 204:
                    return True
                else:
                    logger.error(f"Discord webhook failed with status {response.status_code}: {response.text}")
                    return False

        except httpx.TimeoutException:
            logger.error(f"Discord webhook timeout for: {title}")
            return False
        except Exception as e:
            logger.error(f"Discord webhook error: {e}")
            return False

    async def notify_new_supplier(
        self,
        supplier_name: str,
        supplier_email: Optional[str] = None,
        supplier_phone: Optional[str] = None,
        tax_id: Optional[str] = None,
        payment_terms: Optional[str] = None,
        tenant_name: Optional[str] = None,
        user_name: Optional[str] = None
    ) -> bool:
        """
        Send notification about new supplier creation

        Args:
            supplier_name: Name of the new supplier
            supplier_email: Email of the supplier
            supplier_phone: Phone of the supplier
            tax_id: Tax ID of the supplier
            payment_terms: Payment terms
            tenant_name: Name of the tenant who created the supplier
            user_name: Name of the user who created the supplier

        Returns:
            bool: True if notification sent successfully
        """
        # Build concise description
        info_parts = [f"**Proveedor:** {supplier_name}"]

        if supplier_email:
            info_parts.append(f"**Email:** {supplier_email}")
        if supplier_phone:
            info_parts.append(f"**Tel:** {supplier_phone}")
        if tax_id:
            info_parts.append(f"**NIT:** {tax_id}")

        description = "\n".join(info_parts)

        footer_text = f"{tenant_name or 'N/A'} • {user_name or 'Usuario desconocido'}"

        return await self.send_notification(
            title="Nuevo Proveedor",
            description=description,
            color=5763719,  # Green color
            fields=None,
            footer=footer_text
        )


    async def notify_new_session(
        self,
        user_email: str,
        user_name: str,
        tenant_name: str,
        login_method: str,
        ip_address: str,
        user_agent: str
    ) -> bool:
        """
        Send notification about new session creation (login)

        Args:
            user_email: Email of the user
            user_name: Name of the user
            tenant_name: Name of the tenant
            login_method: Method used for login (magic_link, verification_code)
            ip_address: IP address of the client
            user_agent: User agent of the client

        Returns:
            bool: True if notification sent successfully
        """
        description = (
            f"**Usuario:** {user_name} ({user_email})\n"
            f"**Tenant:** {tenant_name}\n"
            f"**Método:** {login_method}\n"
            f"**IP:** {ip_address}"
        )

        footer_text = f"User Agent: {user_agent[:50]}..." if user_agent else "N/A"

        return await self.send_notification(
            title="Nuevo Inicio de Sesión",
            description=description,
            color=15105570,  # Orange color
            fields=None,
            footer=footer_text
        )


    async def notify_new_purchase(
        self,
        purchase_number: str,
        supplier_name: str,
        total_amount: float,
        created_by_name: str,
        tenant_name: str,
        items_count: int
    ) -> bool:
        """
        Send notification about new purchase creation

        Args:
            purchase_number: Purchase number (e.g. WR-2024-0001)
            supplier_name: Name of the supplier
            total_amount: Total amount of the purchase
            created_by_name: Name of the user who created it
            tenant_name: Name of the tenant
            items_count: Number of items in the purchase

        Returns:
            bool: True if notification sent successfully
        """
        formatted_amount = "{:,.0f}".format(total_amount).replace(",", ".")

        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Monto:** ${formatted_amount} COP\n"
            f"**Items:** {items_count}"
        )

        footer_text = f"{tenant_name} • Creado por {created_by_name}"

        return await self.send_notification(
            title="Nueva Orden de Compra",
            description=description,
            color=10181046,  # Purple color
            fields=None,
            footer=footer_text
        )


    async def notify_supplier_quotation_completed(
        self,
        purchase_number: str,
        supplier_name: str,
        total_amount: float,
        tax_amount: float,
        tenant_name: str,
        items_count: int
    ) -> bool:
        """
        Send notification when supplier completes quotation with prices

        Args:
            purchase_number: Purchase number (e.g. WR-2024-0001)
            supplier_name: Name of the supplier
            total_amount: Total amount of the purchase
            tax_amount: Tax amount
            tenant_name: Name of the tenant
            items_count: Number of items

        Returns:
            bool: True if notification sent successfully
        """
        formatted_total = "{:,.0f}".format(total_amount).replace(",", ".")
        formatted_tax = "{:,.0f}".format(tax_amount).replace(",", ".")

        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Subtotal:** ${formatted_total} COP\n"
            f"**IVA:** ${formatted_tax} COP\n"
            f"**Items:** {items_count}"
        )

        footer_text = f"{tenant_name} • Cotización completada por proveedor"

        return await self.send_notification(
            title="✅ Cotización Completada",
            description=description,
            color=3066993,  # Green color
            fields=None,
            footer=footer_text
        )


    async def notify_supplier_invoice_registered(
        self,
        purchase_number: str,
        supplier_name: str,
        document_type: str,
        invoice_number: str,
        invoice_amount: Optional[float],
        tenant_name: str
    ) -> bool:
        """
        Send notification when supplier registers invoice/remision

        Args:
            purchase_number: Purchase number (e.g. WR-2024-0001)
            supplier_name: Name of the supplier
            document_type: Type of document (remision, factura_contado, factura_credito)
            invoice_number: Invoice/remision number
            invoice_amount: Invoice amount (if applicable)
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        doc_labels = {
            'remision': 'Remisión',
            'factura_contado': 'Factura de Contado',
            'factura_credito': 'Factura a Crédito'
        }
        doc_label = doc_labels.get(document_type, 'Documento')

        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Tipo:** {doc_label}\n"
            f"**Número:** {invoice_number}"
        )

        if invoice_amount is not None:
            formatted_amount = "{:,.0f}".format(invoice_amount).replace(",", ".")
            description += f"\n**Monto:** ${formatted_amount} COP"

        footer_text = f"{tenant_name} • Registrado por proveedor"

        return await self.send_notification(
            title=f"📄 {doc_label} Registrada",
            description=description,
            color=15844367,  # Gold color
            fields=None,
            footer=footer_text
        )


    async def notify_supplier_shipment(
        self,
        purchase_number: str,
        supplier_name: str,
        tracking_number: str,
        carrier: str,
        tenant_name: str,
        package_count: Optional[int] = None
    ) -> bool:
        """
        Send notification when supplier marks purchase as shipped

        Args:
            purchase_number: Purchase number (e.g. WR-2024-0001)
            supplier_name: Name of the supplier
            tracking_number: Tracking number
            carrier: Shipping carrier name
            tenant_name: Name of the tenant
            package_count: Number of packages

        Returns:
            bool: True if notification sent successfully
        """
        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Transportadora:** {carrier}\n"
            f"**Guía:** {tracking_number}"
        )

        if package_count:
            description += f"\n**Paquetes:** {package_count}"

        footer_text = f"{tenant_name} • Marcado como enviado por proveedor"

        return await self.send_notification(
            title="📦 Pedido Enviado",
            description=description,
            color=5793266,  # Blue color
            fields=None,
            footer=footer_text
        )


    # =============================================================================
    # PURCHASE ACTIONS NOTIFICATIONS (Internal staff actions)
    # =============================================================================

    async def notify_purchase_confirmed(
        self,
        purchase_number: str,
        supplier_name: str,
        confirmation_number: Optional[str],
        user_name: str,
        tenant_name: str
    ) -> bool:
        """
        Send notification when purchase is confirmed

        Args:
            purchase_number: Purchase number
            supplier_name: Name of the supplier
            confirmation_number: Confirmation number from supplier
            user_name: Name of the user who confirmed
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}"
        )

        if confirmation_number:
            description += f"\n**Confirmación:** {confirmation_number}"

        footer_text = f"{tenant_name} • Confirmado por {user_name}"

        return await self.send_notification(
            title="✅ Orden Confirmada",
            description=description,
            color=3066993,  # Green color
            fields=None,
            footer=footer_text
        )

    async def notify_purchase_shipped(
        self,
        purchase_number: str,
        supplier_name: str,
        tracking_number: str,
        carrier: str,
        user_name: str,
        tenant_name: str
    ) -> bool:
        """
        Send notification when purchase is shipped (internal action)

        Args:
            purchase_number: Purchase number
            supplier_name: Name of the supplier
            tracking_number: Tracking number
            carrier: Carrier name
            user_name: Name of the user who shipped
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Transportadora:** {carrier}\n"
            f"**Guía:** {tracking_number}"
        )

        footer_text = f"{tenant_name} • Registrado por {user_name}"

        return await self.send_notification(
            title="🚚 Orden Enviada",
            description=description,
            color=3447003,  # Blue color
            fields=None,
            footer=footer_text
        )

    async def notify_purchase_received(
        self,
        purchase_number: str,
        supplier_name: str,
        is_partial: bool,
        user_name: str,
        tenant_name: str
    ) -> bool:
        """
        Send notification when purchase is received

        Args:
            purchase_number: Purchase number
            supplier_name: Name of the supplier
            is_partial: True if partial reception
            user_name: Name of the user who received
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        title = "📥 Orden Recibida Parcialmente" if is_partial else "📥 Orden Recibida"

        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}"
        )

        if is_partial:
            description += "\n**Tipo:** Recepción parcial"

        footer_text = f"{tenant_name} • Recibido por {user_name}"

        return await self.send_notification(
            title=title,
            description=description,
            color=10181046,  # Purple color
            fields=None,
            footer=footer_text
        )

    async def notify_purchase_invoiced(
        self,
        purchase_number: str,
        supplier_name: str,
        invoice_number: str,
        invoice_amount: Optional[float],
        user_name: str,
        tenant_name: str
    ) -> bool:
        """
        Send notification when purchase is invoiced (internal action)

        Args:
            purchase_number: Purchase number
            supplier_name: Name of the supplier
            invoice_number: Invoice number
            invoice_amount: Invoice amount
            user_name: Name of the user who invoiced
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Factura:** {invoice_number}"
        )

        if invoice_amount is not None:
            formatted_amount = "{:,.0f}".format(invoice_amount).replace(",", ".")
            description += f"\n**Monto:** ${formatted_amount} COP"

        footer_text = f"{tenant_name} • Registrado por {user_name}"

        return await self.send_notification(
            title="📄 Orden Facturada",
            description=description,
            color=15844367,  # Gold color
            fields=None,
            footer=footer_text
        )

    async def notify_purchase_paid(
        self,
        purchase_number: str,
        supplier_name: str,
        payment_method: str,
        payment_amount: float,
        user_name: str,
        tenant_name: str
    ) -> bool:
        """
        Send notification when purchase is paid

        Args:
            purchase_number: Purchase number
            supplier_name: Name of the supplier
            payment_method: Payment method used
            payment_amount: Amount paid
            user_name: Name of the user who registered payment
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        formatted_amount = "{:,.0f}".format(payment_amount).replace(",", ".")

        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}\n"
            f"**Método:** {payment_method}\n"
            f"**Monto:** ${formatted_amount} COP"
        )

        footer_text = f"{tenant_name} • Pagado por {user_name}"

        return await self.send_notification(
            title="💰 Orden Pagada",
            description=description,
            color=3066993,  # Green color
            fields=None,
            footer=footer_text
        )

    async def notify_purchase_cancelled(
        self,
        purchase_number: str,
        supplier_name: str,
        reason: Optional[str],
        user_name: str,
        tenant_name: str
    ) -> bool:
        """
        Send notification when purchase is cancelled

        Args:
            purchase_number: Purchase number
            supplier_name: Name of the supplier
            reason: Cancellation reason
            user_name: Name of the user who cancelled
            tenant_name: Name of the tenant

        Returns:
            bool: True if notification sent successfully
        """
        description = (
            f"**Orden:** {purchase_number}\n"
            f"**Proveedor:** {supplier_name}"
        )

        if reason:
            description += f"\n**Razón:** {reason}"

        footer_text = f"{tenant_name} • Cancelado por {user_name}"

        return await self.send_notification(
            title="❌ Orden Cancelada",
            description=description,
            color=15158332,  # Red color
            fields=None,
            footer=footer_text
        )


    async def notify_new_lead(
        self,
        email: str,
        phone: str,
        button_source: str,
        ip_address: Optional[str] = None,
    ) -> bool:
        """Send notification when a new lead is captured from the homepage CTA."""
        button_labels = {
            "comenzar": "Comenzar",
            "habla_con_nosotros": "Habla con nosotros",
        }
        button_label = button_labels.get(button_source, button_source)

        description = (
            f"**Email:** {email}\n"
            f"**Teléfono:** +57 {phone}\n"
            f"**Botón:** {button_label}"
        )
        if ip_address:
            description += f"\n**IP:** {ip_address}"

        return await self.send_notification(
            title="🎯 Nuevo Lead Capturado",
            description=description,
            color=5763719,  # Green
        )


# Singleton instance with webhook URL from settings
from app.config import settings

# Only initialize if webhook URL is configured
discord_service = None
if settings.discord_webhook_url:
    discord_service = DiscordWebhookService(settings.discord_webhook_url)
else:
    logger.warning("Discord webhook URL not configured - notifications disabled")

# Separate service for session notifications
discord_session_service = None
if settings.discord_session_webhook_url:
    discord_session_service = DiscordWebhookService(settings.discord_session_webhook_url)
else:
    logger.warning("Discord session webhook URL not configured - session notifications disabled")

# Separate service for purchase notifications
discord_purchase_service = None
if settings.discord_purchase_webhook_url:
    discord_purchase_service = DiscordWebhookService(settings.discord_purchase_webhook_url)
else:
    logger.warning("Discord purchase webhook URL not configured - purchase notifications disabled")

# Separate service for supplier portal notifications
discord_supplier_service = None
if settings.discord_supplier_webhook_url:
    discord_supplier_service = DiscordWebhookService(settings.discord_supplier_webhook_url)
else:
    logger.warning("Discord supplier webhook URL not configured - supplier notifications disabled")

# Separate service for purchase actions notifications (confirm, ship, receive, invoice, pay, cancel)
discord_purchase_actions_service = None
if settings.discord_purchase_actions_webhook_url:
    discord_purchase_actions_service = DiscordWebhookService(settings.discord_purchase_actions_webhook_url)
else:
    logger.warning("Discord purchase actions webhook URL not configured - purchase actions notifications disabled")

# Separate service for lead capture notifications (homepage CTA)
discord_leads_service = None
if settings.discord_leads_webhook_url:
    discord_leads_service = DiscordWebhookService(settings.discord_leads_webhook_url)
else:
    logger.warning("Discord leads webhook URL not configured - lead notifications disabled")
