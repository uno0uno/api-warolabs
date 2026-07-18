import logging
import boto3
from botocore.exceptions import ClientError
from typing import List, Optional
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
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
        html_body: Optional[str] = None,
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

        if not html_body and not text_body:
            logger.error("❌ Either html_body or text_body must be provided")
            return False

        try:
            # Prepare source field with optional name
            source = f"{from_name} <{from_email}>" if from_name else from_email

            # Prepare message body - only include parts that are provided
            message_body = {}

            if html_body:
                message_body['Html'] = {
                    'Charset': 'UTF-8',
                    'Data': html_body,
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

    async def send_email_with_attachment(
        self,
        from_email: str,
        from_name: Optional[str] = None,
        to_emails: List[str] = None,
        subject: str = "",
        text_body: Optional[str] = None,
        attachment_data: bytes = None,
        attachment_filename: str = "attachment.xml",
        attachment_type: str = "application/xml"
    ) -> bool:
        """
        Send email with attachment using AWS SES raw email
        """
        if not self.client:
            logger.error("❌ AWS SES client not initialized - cannot send email")
            return False

        if not to_emails:
            logger.error("❌ No recipient email addresses provided")
            return False

        try:
            # Create multipart message
            msg = MIMEMultipart('mixed')

            # Set headers
            source = f"{from_name} <{from_email}>" if from_name else from_email
            msg['From'] = source
            msg['To'] = ', '.join(to_emails)
            msg['Subject'] = subject

            # Add text body
            if text_body:
                text_part = MIMEText(text_body, 'plain', 'utf-8')
                msg.attach(text_part)

            # Add attachment
            if attachment_data:
                attachment = MIMEApplication(attachment_data)
                attachment.add_header(
                    'Content-Disposition',
                    'attachment',
                    filename=attachment_filename
                )
                attachment.add_header('Content-Type', attachment_type)
                msg.attach(attachment)

            # Send raw email
            response = self.client.send_raw_email(
                Source=source,
                Destinations=to_emails,
                RawMessage={'Data': msg.as_string()}
            )

            message_id = response['MessageId']
            logger.info(f"✅ Email with attachment sent successfully. MessageId: {message_id}")
            logger.info(f"📧 From: {source} | To: {', '.join(to_emails)} | Subject: {subject} | Attachment: {attachment_filename}")

            return True

        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_message = e.response['Error']['Message']
            logger.error(f"❌ AWS SES ClientError: {error_code} - {error_message}")
            return False

        except Exception as e:
            logger.error(f"❌ Failed to send email with attachment: {e}")
            return False

    async def send_email_with_attachments(
        self,
        from_email: str,
        from_name: Optional[str] = None,
        to_emails: List[str] = None,
        subject: str = "",
        text_body: Optional[str] = None,
        html_body: Optional[str] = None,
        attachments: Optional[List[dict]] = None,
    ) -> bool:
        """
        Send email with multiple attachments using AWS SES raw email.

        Body is wrapped as multipart/alternative (text + html) inside the
        multipart/mixed envelope, so clients pick the best renderable part.

        attachments: list of { data: bytes, filename: str, content_type: str }
        """
        if not self.client:
            logger.error("❌ AWS SES client not initialized - cannot send email")
            return False

        if not to_emails:
            logger.error("❌ No recipient email addresses provided")
            return False

        try:
            msg = MIMEMultipart('mixed')
            source = f"{from_name} <{from_email}>" if from_name else from_email
            msg['From'] = source
            msg['To'] = ', '.join(to_emails)
            msg['Subject'] = subject

            if html_body:
                alternative = MIMEMultipart('alternative')
                if text_body:
                    alternative.attach(MIMEText(text_body, 'plain', 'utf-8'))
                alternative.attach(MIMEText(html_body, 'html', 'utf-8'))
                msg.attach(alternative)
            elif text_body:
                msg.attach(MIMEText(text_body, 'plain', 'utf-8'))

            for att in (attachments or []):
                data = att.get("data")
                filename = att.get("filename") or "attachment"
                content_type = att.get("content_type") or "application/octet-stream"
                if not data:
                    continue
                part = MIMEApplication(data)
                part.add_header('Content-Disposition', 'attachment', filename=filename)
                part.add_header('Content-Type', content_type)
                msg.attach(part)

            response = self.client.send_raw_email(
                Source=source,
                Destinations=to_emails,
                RawMessage={'Data': msg.as_string()}
            )

            message_id = response['MessageId']
            logger.info(
                f"✅ Email with attachments sent successfully. MessageId: {message_id} "
                f"| Attachment count: {len(attachments or [])}"
            )
            return True

        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_message = e.response['Error']['Message']
            logger.error(f"❌ AWS SES ClientError: {error_code} - {error_message}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to send email with attachments: {e}")
            return False

# Global instance
ses_service = AWSSESService()