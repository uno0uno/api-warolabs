"""
Purchase Attachment Models
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from uuid import UUID
from datetime import datetime
from decimal import Decimal

class PurchaseAttachmentBase(BaseModel):
    """Base purchase attachment model"""
    file_name: str = Field(..., description="Original filename")
    file_type: str = Field(..., description="MIME type (e.g., application/pdf)")
    file_size: int = Field(..., gt=0, description="File size in bytes")
    attachment_type: Literal["invoice", "receipt", "contract", "delivery_note", "quotation", "other"] = Field(
        ...,
        description="Type of attachment"
    )
    description: Optional[str] = Field(None, description="Optional description of the attachment")

class PurchaseAttachmentCreate(PurchaseAttachmentBase):
    """Model for creating a purchase attachment"""
    purchase_id: UUID = Field(..., description="Associated purchase ID")
    s3_key: str = Field(..., description="S3 key/path to the file")
    s3_url: Optional[str] = Field(None, description="Full S3 URL if needed")

class PurchaseAttachment(PurchaseAttachmentBase):
    """Full purchase attachment model"""
    id: UUID
    tenant_id: UUID
    purchase_id: UUID
    s3_key: str
    s3_url: Optional[str] = None
    uploaded_at: datetime
    uploaded_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class PurchaseAttachmentResponse(BaseModel):
    """Response model for attachment operations"""
    success: bool
    message: str
    data: Optional[PurchaseAttachment] = None

class PurchaseAttachmentsListResponse(BaseModel):
    """Response model for listing attachments"""
    success: bool
    data: list[PurchaseAttachment]
    total: int

class FileUploadResponse(BaseModel):
    """Response model for file upload"""
    success: bool
    message: str
    s3_key: Optional[str] = None
    file_url: Optional[str] = None
