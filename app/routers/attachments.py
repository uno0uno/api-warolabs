"""
Purchase Attachments Router
API endpoints for managing purchase attachments
"""
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from typing import Optional
from uuid import UUID
from app.models.attachment import (
    PurchaseAttachmentsListResponse,
    PurchaseAttachmentResponse,
    FileUploadResponse
)
from app.services.attachments_service import (
    upload_purchase_attachment,
    get_purchase_attachments,
    delete_purchase_attachment
)

router = APIRouter(prefix="/attachments", tags=["attachments"])

@router.post("/purchases/{purchase_id}/upload", response_model=FileUploadResponse)
async def upload_attachment(
    request: Request,
    purchase_id: UUID,
    file: UploadFile = File(..., description="File to upload (max 10MB)"),
    attachment_type: str = Form(..., description="Type: invoice, receipt, contract, delivery_note, quotation, other"),
    description: Optional[str] = Form(None, description="Optional description of the attachment")
):
    """
    Upload a file attachment for a purchase

    **Allowed file types:**
    - PDF documents (application/pdf)
    - Images (image/jpeg, image/png, image/gif)
    - Excel/CSV (application/vnd.ms-excel, text/csv)
    - Word documents (application/msword)

    **Maximum file size:** 10MB

    **Attachment types:**
    - invoice: Invoice document
    - receipt: Payment receipt
    - contract: Contract or agreement
    - delivery_note: Delivery note/remision
    - quotation: Supplier quotation
    - other: Other supporting documents
    """
    # Validate file size (10MB max)
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB in bytes
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File size exceeds maximum allowed size of 10MB"
        )

    # Validate attachment type
    valid_types = ['invoice', 'receipt', 'contract', 'delivery_note', 'quotation', 'other']
    if attachment_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid attachment type. Must be one of: {', '.join(valid_types)}"
        )

    return await upload_purchase_attachment(
        request=request,
        purchase_id=purchase_id,
        file=file,
        attachment_type=attachment_type,
        description=description
    )


@router.get("/purchases/{purchase_id}", response_model=PurchaseAttachmentsListResponse)
async def list_attachments(
    request: Request,
    purchase_id: UUID,
    attachment_type: Optional[str] = None
):
    """
    Get all attachments for a purchase

    **Optional filters:**
    - attachment_type: Filter by attachment type (invoice, receipt, etc.)

    Returns a list of attachments with presigned URLs for downloading.
    URLs expire after 1 hour.
    """
    return await get_purchase_attachments(
        request=request,
        purchase_id=purchase_id,
        attachment_type=attachment_type
    )


@router.delete("/{attachment_id}", response_model=PurchaseAttachmentResponse)
async def delete_attachment(
    request: Request,
    attachment_id: UUID
):
    """
    Delete an attachment

    Deletes both the file from S3 and the database record.
    This action cannot be undone.
    """
    return await delete_purchase_attachment(
        request=request,
        attachment_id=attachment_id
    )
