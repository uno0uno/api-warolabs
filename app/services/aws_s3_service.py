"""
AWS S3 Service for file uploads
Handles uploading, downloading, and deleting files from S3
"""
import boto3
from botocore.exceptions import ClientError
from typing import Optional, BinaryIO
from datetime import datetime, timedelta
from app.config import settings
import uuid
import mimetypes

class AWSS3Service:
    def __init__(self):
        """Initialize S3 client"""
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region
        )
        self.bucket_name = settings.aws_s3_bucket

    async def upload_file(
        self,
        file_content: BinaryIO,
        filename: str,
        folder: str = 'purchases',
        content_type: Optional[str] = None
    ) -> Optional[str]:
        """
        Upload a file to S3

        Args:
            file_content: File binary content
            filename: Original filename
            folder: Folder path in bucket (e.g., 'purchases', 'invoices')
            content_type: MIME type of the file

        Returns:
            S3 key (path) if successful, None if failed
        """
        try:
            # Generate unique filename to avoid collisions
            file_extension = filename.split('.')[-1] if '.' in filename else ''
            unique_filename = f"{uuid.uuid4()}.{file_extension}" if file_extension else str(uuid.uuid4())

            # Build S3 key (path)
            timestamp = datetime.now().strftime('%Y/%m/%d')
            s3_key = f"{folder}/{timestamp}/{unique_filename}"

            # Detect content type if not provided
            if not content_type:
                content_type, _ = mimetypes.guess_type(filename)
                if not content_type:
                    content_type = 'application/octet-stream'

            # Upload to S3
            self.s3_client.upload_fileobj(
                file_content,
                self.bucket_name,
                s3_key,
                ExtraArgs={
                    'ContentType': content_type,
                    'Metadata': {
                        'original_filename': filename,
                        'uploaded_at': datetime.now().isoformat()
                    }
                }
            )

            print(f"✅ File uploaded successfully to S3: {s3_key}")
            return s3_key

        except ClientError as e:
            print(f"❌ Error uploading file to S3: {str(e)}")
            return None
        except Exception as e:
            print(f"❌ Unexpected error uploading file: {str(e)}")
            return None

    async def get_presigned_url(
        self,
        s3_key: str,
        expiration: int = 3600
    ) -> Optional[str]:
        """
        Generate a presigned URL for downloading a file

        Args:
            s3_key: S3 key (path) of the file
            expiration: URL expiration time in seconds (default: 1 hour)

        Returns:
            Presigned URL if successful, None if failed
        """
        try:
            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': s3_key
                },
                ExpiresIn=expiration
            )
            return url
        except ClientError as e:
            print(f"❌ Error generating presigned URL: {str(e)}")
            return None

    async def delete_file(self, s3_key: str) -> bool:
        """
        Delete a file from S3

        Args:
            s3_key: S3 key (path) of the file to delete

        Returns:
            True if successful, False if failed
        """
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            print(f"✅ File deleted successfully from S3: {s3_key}")
            return True
        except ClientError as e:
            print(f"❌ Error deleting file from S3: {str(e)}")
            return False

    async def get_file_metadata(self, s3_key: str) -> Optional[dict]:
        """
        Get metadata of a file in S3

        Args:
            s3_key: S3 key (path) of the file

        Returns:
            Dictionary with file metadata if successful, None if failed
        """
        try:
            response = self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            return {
                'content_type': response.get('ContentType'),
                'content_length': response.get('ContentLength'),
                'last_modified': response.get('LastModified'),
                'metadata': response.get('Metadata', {})
            }
        except ClientError as e:
            print(f"❌ Error getting file metadata: {str(e)}")
            return None
