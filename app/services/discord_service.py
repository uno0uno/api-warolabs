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
