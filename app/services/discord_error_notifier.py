"""
Discord Error Notification Service
Sends error notifications to Discord webhook for real-time monitoring
"""
import aiohttp
import traceback
import sys
from datetime import datetime
from typing import Optional, Dict, Any
import logging
from app.config import settings

logger = logging.getLogger(__name__)

class DiscordErrorNotifier:
    """Send error notifications to Discord webhook"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def send_error(
        self,
        error: Exception,
        context: Optional[Dict[str, Any]] = None,
        request_info: Optional[Dict[str, Any]] = None
    ):
        """
        Send error notification to Discord

        Args:
            error: The exception that occurred
            context: Additional context about the error
            request_info: Information about the request that caused the error
        """
        try:
            # Get error details
            error_type = type(error).__name__
            error_message = str(error)
            error_traceback = ''.join(traceback.format_exception(type(error), error, error.__traceback__))

            # Truncate traceback if too long (Discord has 2000 char limit per field)
            if len(error_traceback) > 1900:
                error_traceback = error_traceback[:1900] + "\n... (truncated)"

            # Build embed
            embed = {
                "title": f"🚨 Error: {error_type}",
                "description": error_message[:2000] if error_message else "No error message",
                "color": 15158332,  # Red color
                "timestamp": datetime.utcnow().isoformat(),
                "fields": []
            }

            # Add request info if available
            if request_info:
                request_details = []
                if request_info.get('method'):
                    request_details.append(f"**Method:** {request_info['method']}")
                if request_info.get('url'):
                    request_details.append(f"**URL:** {request_info['url']}")
                if request_info.get('client_host'):
                    request_details.append(f"**Client:** {request_info['client_host']}")
                if request_info.get('user_agent'):
                    request_details.append(f"**User Agent:** {request_info['user_agent'][:100]}")

                if request_details:
                    embed["fields"].append({
                        "name": "📡 Request Info",
                        "value": "\n".join(request_details),
                        "inline": False
                    })

            # Add context if available
            if context:
                context_details = []
                for key, value in context.items():
                    context_details.append(f"**{key}:** {value}")

                if context_details:
                    embed["fields"].append({
                        "name": "🔍 Context",
                        "value": "\n".join(context_details)[:1024],
                        "inline": False
                    })

            # Add traceback
            embed["fields"].append({
                "name": "📋 Traceback",
                "value": f"```python\n{error_traceback}\n```"[:1024],
                "inline": False
            })

            # Add environment info
            embed["fields"].append({
                "name": "🌍 Environment",
                "value": f"**Env:** {settings.environment}\n**Base URL:** {settings.base_url}",
                "inline": True
            })

            # Build payload
            payload = {
                "embeds": [embed],
                "username": "API Error Monitor",
                "avatar_url": "https://cdn-icons-png.flaticon.com/512/753/753345.png"
            }

            # Send to Discord
            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload) as response:
                    if response.status != 204:
                        logger.warning(f"Failed to send error to Discord: {response.status}")
                    else:
                        logger.info(f"Error notification sent to Discord: {error_type}")

        except Exception as e:
            # Don't let error notification failures crash the app
            logger.error(f"Failed to send error notification to Discord: {str(e)}")
            logger.exception(e)

    async def send_warning(
        self,
        title: str,
        message: str,
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Send warning notification to Discord

        Args:
            title: Warning title
            message: Warning message
            context: Additional context
        """
        try:
            embed = {
                "title": f"⚠️ {title}",
                "description": message[:2000],
                "color": 16776960,  # Yellow color
                "timestamp": datetime.utcnow().isoformat()
            }

            if context:
                fields = []
                for key, value in context.items():
                    fields.append({
                        "name": key,
                        "value": str(value)[:1024],
                        "inline": True
                    })
                embed["fields"] = fields

            payload = {
                "embeds": [embed],
                "username": "API Warning Monitor",
                "avatar_url": "https://cdn-icons-png.flaticon.com/512/753/753345.png"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload) as response:
                    if response.status == 204:
                        logger.info(f"Warning notification sent to Discord: {title}")

        except Exception as e:
            logger.error(f"Failed to send warning to Discord: {str(e)}")

    async def send_info(
        self,
        title: str,
        message: str,
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Send info notification to Discord

        Args:
            title: Info title
            message: Info message
            context: Additional context
        """
        try:
            embed = {
                "title": f"ℹ️ {title}",
                "description": message[:2000],
                "color": 3447003,  # Blue color
                "timestamp": datetime.utcnow().isoformat()
            }

            if context:
                fields = []
                for key, value in context.items():
                    fields.append({
                        "name": key,
                        "value": str(value)[:1024],
                        "inline": True
                    })
                embed["fields"] = fields

            payload = {
                "embeds": [embed],
                "username": "API Info Monitor",
                "avatar_url": "https://cdn-icons-png.flaticon.com/512/753/753345.png"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload) as response:
                    if response.status == 204:
                        logger.info(f"Info notification sent to Discord: {title}")

        except Exception as e:
            logger.error(f"Failed to send info to Discord: {str(e)}")


# Global error notifier instance
# Read webhook URL from settings
error_notifier = DiscordErrorNotifier(settings.discord_error_webhook_url)
