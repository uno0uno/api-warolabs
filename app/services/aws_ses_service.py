import logging
import boto3
from botocore.exceptions import ClientError
from typing import List, Optional
from app.config import settings

logger = logging.getLogger(__name__)

class AWSSESService:
    """AWS SES email service compatible with warolabs.com email system"""
    
    def __init__(self):
        self.client = None
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize AWS SES client with credentials from settings"""
        try:
            if not all([settings.aws_access_key_id, settings.aws_secret_access_key, settings.aws_region]):
                logger.warning("⚠️ AWS SES credentials not configured - email sending disabled")
                return
            
            self.client = boto3.client(
                'ses',
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_region
            )
            logger.info(f"✅ AWS SES client initialized for region: {settings.aws_region}")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize AWS SES client: {e}")
            self.client = None
    
    async def send_email(
        self, 
        from_email: str,
        from_name: Optional[str] = None,
        to_emails: List[str] = None,
        subject: str = "",
        html_body: str = "",
        text_body: Optional[str] = None
    ) -> bool:
        """
        Send email using AWS SES
        Compatible with warolabs.com sendEmail function parameters
        """
        if not self.client:
            logger.error("❌ AWS SES client not initialized - cannot send email")
            return False
        
        if not to_emails:
            logger.error("❌ No recipient email addresses provided")
            return False
        
        try:
            # Prepare source field with optional name
            source = f"{from_name} <{from_email}>" if from_name else from_email
            
            # Prepare message body
            message_body = {
                'Html': {
                    'Charset': 'UTF-8',
                    'Data': html_body,
                }
            }
            
            if text_body:
                message_body['Text'] = {
                    'Charset': 'UTF-8',
                    'Data': text_body,
                }
            
            # Send email
            response = self.client.send_email(
                Source=source,
                Destination={
                    'ToAddresses': to_emails,
                },
                Message={
                    'Subject': {
                        'Charset': 'UTF-8',
                        'Data': subject,
                    },
                    'Body': message_body,
                }
            )
            
            message_id = response['MessageId']
            logger.info(f"✅ Email sent successfully. MessageId: {message_id}")
            logger.info(f"📧 From: {source} | To: {', '.join(to_emails)} | Subject: {subject}")
            
            return True
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_message = e.response['Error']['Message']
            logger.error(f"❌ AWS SES ClientError: {error_code} - {error_message}")
            return False
            
        except Exception as e:
            logger.error(f"❌ Failed to send email: {e}")
            return False

# Global instance
ses_service = AWSSESService()