"""
Purchase Attachments Service
Handles CRUD operations for purchase attachments
"""
from typing import List, Optional
from uuid import UUID
from fastapi import HTTPException, UploadFile
from app.database import get_db_connection
from app.models.attachment import (
    PurchaseAttachment,
    PurchaseAttachmentCreate,
    PurchaseAttachmentResponse,
    PurchaseAttachmentsListResponse,
    FileUploadResponse
)
from app.services.aws_s3_service import AWSS3Service
from app.core.security import get_session_from_request
from fastapi import Request

async def upload_purchase_attachment(
    request: Request,
    purchase_id: UUID,
    file: UploadFile,
    attachment_type: str,
    description: Optional[str] = None
) -> FileUploadResponse:
    """
    Upload a file attachment for a purchase

    Args:
        request: FastAPI request (for auth)
        purchase_id: Purchase ID
        file: Uploaded file
        attachment_type: Type of attachment
        description: Optional description

    Returns:
        FileUploadResponse with upload status
    """
    try:
        # Get current user from session
        session = await get_session_from_request(request)
        if not session:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_id = session['user_id']
        tenant_id = session['tenant_id']

        # Verify purchase exists and belongs to tenant
        async with get_db_connection() as conn:
            purchase = await conn.fetchrow("""
                SELECT id FROM tenant_purchases
                WHERE id = $1 AND tenant_id = $2
            """, purchase_id, tenant_id)

            if not purchase:
                raise HTTPException(status_code=404, detail="Purchase not found")

        # Upload file to S3
        s3_service = AWSS3Service()
        s3_key = await s3_service.upload_file(
            file_content=file.file,
            filename=file.filename,
            folder='purchases/attachments',
            content_type=file.content_type
        )

        if not s3_key:
            raise HTTPException(status_code=500, detail="Failed to upload file to S3")

        # Generate presigned URL for immediate access
        file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)

        # Save attachment record to database
        async with get_db_connection() as conn:
            attachment = await conn.fetchrow("""
                INSERT INTO purchase_attachments (
                    tenant_id,
                    purchase_id,
                    file_name,
                    file_type,
                    file_size,
                    s3_key,
                    s3_url,
                    attachment_type,
                    description,
                    uploaded_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
            """,
                tenant_id,
                purchase_id,
                file.filename,
                file.content_type or 'application/octet-stream',
                file.size or 0,
                s3_key,
                file_url,
                attachment_type,
                description,
                user_id
            )

        return FileUploadResponse(
            success=True,
            message="File uploaded successfully",
            s3_key=s3_key,
            file_url=file_url
        )

    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error uploading attachment")

async def get_purchase_attachments(
    request: Request,
    purchase_id: UUID,
    attachment_type: Optional[str] = None
) -> PurchaseAttachmentsListResponse:
    """
    Get all attachments for a purchase

    Args:
        request: FastAPI request (for auth)
        purchase_id: Purchase ID
        attachment_type: Optional filter by attachment type

    Returns:
        List of attachments
    """
    try:
        # Get current user from session
        session = await get_session_from_request(request)
        if not session:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_id = session['user_id']
        tenant_id = session['tenant_id']

        # Build query
        query = """
            SELECT * FROM purchase_attachments
            WHERE purchase_id = $1 AND tenant_id = $2
        """
        params = [purchase_id, tenant_id]

        if attachment_type:
            query += " AND attachment_type = $3"
            params.append(attachment_type)

        query += " ORDER BY uploaded_at DESC"

        async with get_db_connection() as conn:
            rows = await conn.fetch(query, *params)

        # Generate presigned URLs for each attachment
        s3_service = AWSS3Service()
        attachments = []
        for row in rows:
            # Generate fresh presigned URL
            presigned_url = await s3_service.get_presigned_url(row['s3_key'], expiration=3600)

            attachment = PurchaseAttachment(
                id=row['id'],
                tenant_id=row['tenant_id'],
                purchase_id=row['purchase_id'],
                file_name=row['file_name'],
                file_type=row['file_type'],
                file_size=row['file_size'],
                s3_key=row['s3_key'],
                s3_url=presigned_url,
                attachment_type=row['attachment_type'],
                description=row['description'],
                uploaded_at=row['uploaded_at'],
                uploaded_by=row['uploaded_by'],
                created_at=row['created_at'],
                updated_at=row['updated_at']
            )
            attachments.append(attachment)

        return PurchaseAttachmentsListResponse(
            success=True,
            data=attachments,
            total=len(attachments)
        )

    except Exception as e:

        raise HTTPException(status_code=500, detail="Error getting attachments")

async def delete_purchase_attachment(
    request: Request,
    attachment_id: UUID
) -> PurchaseAttachmentResponse:
    """
    Delete an attachment

    Args:
        request: FastAPI request (for auth)
        attachment_id: Attachment ID to delete

    Returns:
        Response with deletion status
    """
    try:
        # Get current user from session
        session = await get_session_from_request(request)
        if not session:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_id = session['user_id']
        tenant_id = session['tenant_id']

        # Get attachment and verify it belongs to tenant
        async with get_db_connection() as conn:
            attachment = await conn.fetchrow("""
                SELECT * FROM purchase_attachments
                WHERE id = $1 AND tenant_id = $2
            """, attachment_id, tenant_id)

            if not attachment:
                raise HTTPException(status_code=404, detail="Attachment not found")

            # Delete file from S3
            s3_service = AWSS3Service()
            await s3_service.delete_file(attachment['s3_key'])

            # Delete attachment record from database
            await conn.execute("""
                DELETE FROM purchase_attachments
                WHERE id = $1
            """, attachment_id)

        return PurchaseAttachmentResponse(
            success=True,
            message="Attachment deleted successfully"
        )

    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error deleting attachment")
