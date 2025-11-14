from pydantic import BaseModel, Field
from datetime import datetime, date
from typing import Optional, List, Literal
from uuid import UUID
from decimal import Decimal
from enum import Enum

# =============================================================================
# ENUMS FOR STATUS AND CONDITIONS
# =============================================================================

class PurchaseStatus(str, Enum):
    """Purchase order status states"""
    QUOTATION = "quotation"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    SHIPPED = "shipped"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    VERIFIED = "verified"
    INVOICED = "invoiced"
    PAID = "paid"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"

class QualityStatus(str, Enum):
    """Quality assessment status"""
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR = "poor"
    REJECTED = "rejected"

class ItemCondition(str, Enum):
    """Item reception condition"""
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"
    DAMAGED = "damaged"

class PackageCondition(str, Enum):
    """Package condition on arrival"""
    GOOD = "good"
    DAMAGED = "damaged"
    PARTIAL = "partial"

class PaymentMethod(str, Enum):
    """Payment method types"""
    TRANSFER = "transfer"
    CHECK = "check"
    CASH = "cash"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    OTHER = "other"

class AttachmentType(str, Enum):
    """Attachment file types"""
    PURCHASE_ORDER = "purchase_order"
    CONFIRMATION = "confirmation"
    SHIPPING_LABEL = "shipping_label"
    INVOICE = "invoice"
    PAYMENT_PROOF = "payment_proof"
    QUALITY_PHOTO = "quality_photo"
    DELIVERY_PHOTO = "delivery_photo"
    OTHER = "other"

# =============================================================================
# PURCHASE ITEM MODELS
# =============================================================================

class PurchaseItemBase(BaseModel):
    ingredient_id: UUID = Field(..., description="ID of the ingredient")
    quantity: Decimal = Field(..., gt=0, description="Quantity purchased")
    unit: str = Field(..., description="Unit of measure")
    unit_cost: Optional[Decimal] = Field(None, ge=0, description="Cost per unit (optional for quotations)")
    total_cost: Optional[Decimal] = Field(None, description="Total cost (calculated)")
    expiry_date: Optional[date] = Field(None, description="Expiry date of the ingredient")
    batch_number: Optional[str] = Field(None, description="Batch or lot number")
    notes: Optional[str] = Field(None, description="Additional notes")

class PurchaseItemCreate(PurchaseItemBase):
    """Model for creating purchase items"""
    pass

class PurchaseItemUpdate(BaseModel):
    """Model for updating purchase items"""
    ingredient_id: Optional[UUID] = None
    quantity: Optional[Decimal] = Field(None, gt=0)
    unit: Optional[str] = None
    unit_cost: Optional[Decimal] = Field(None, gt=0)
    total_cost: Optional[Decimal] = None
    expiry_date: Optional[date] = None
    batch_number: Optional[str] = None
    notes: Optional[str] = None
    # Reception fields
    quantity_received: Optional[Decimal] = Field(None, ge=0, description="Quantity actually received")
    quality_status: Optional[QualityStatus] = Field(None, description="Quality assessment")
    quality_notes: Optional[str] = Field(None, description="Quality inspection notes")
    verification_notes: Optional[str] = Field(None, description="Verification notes")
    item_condition: Optional[ItemCondition] = Field(None, description="Item condition")
    received_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None

class PurchaseItem(PurchaseItemBase):
    """Full purchase item model with all fields"""
    id: UUID
    purchase_id: UUID
    created_at: Optional[datetime] = None
    # Reception fields
    quantity_received: Optional[Decimal] = None
    quality_status: Optional[QualityStatus] = None
    quality_notes: Optional[str] = None
    verification_notes: Optional[str] = None
    item_condition: Optional[ItemCondition] = None
    received_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        use_enum_values = True

# =============================================================================
# PURCHASE STATUS HISTORY MODELS
# =============================================================================

class PurchaseStatusHistoryBase(BaseModel):
    """Base model for purchase status history"""
    from_status: Optional[PurchaseStatus] = Field(None, description="Previous status")
    to_status: PurchaseStatus = Field(..., description="New status")
    metadata: Optional[dict] = Field(default_factory=dict, description="State-specific metadata")
    notes: Optional[str] = Field(None, description="Transition notes")

class PurchaseStatusHistoryCreate(PurchaseStatusHistoryBase):
    """Model for creating status history entry"""
    purchase_id: UUID
    tenant_id: UUID
    changed_by: UUID

class PurchaseStatusHistory(PurchaseStatusHistoryBase):
    """Full status history model"""
    id: UUID
    purchase_id: UUID
    tenant_id: UUID
    changed_by: UUID
    changed_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True

# =============================================================================
# PURCHASE ATTACHMENT MODELS
# =============================================================================

class PurchaseAttachmentBase(BaseModel):
    """Base model for purchase attachments"""
    path: str = Field(..., description="Cloudflare R2 path/URL")
    file_name: str = Field(..., description="Original file name")
    file_size: Optional[int] = Field(None, description="File size in bytes")
    mime_type: Optional[str] = Field(None, description="MIME type")
    attachment_type: AttachmentType = Field(..., description="Type of attachment")
    related_status: Optional[PurchaseStatus] = Field(None, description="Related purchase status")
    description: Optional[str] = Field(None, description="File description")

class PurchaseAttachmentCreate(PurchaseAttachmentBase):
    """Model for creating attachments"""
    purchase_id: UUID
    tenant_id: UUID
    uploaded_by: UUID

class PurchaseAttachment(PurchaseAttachmentBase):
    """Full attachment model"""
    id: UUID
    purchase_id: UUID
    tenant_id: UUID
    uploaded_by: UUID
    uploaded_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True

# =============================================================================
# PURCHASE ORDER MODELS
# =============================================================================

class PurchaseBase(BaseModel):
    """Base purchase order model"""
    supplier_id: Optional[UUID] = Field(None, description="Supplier ID")
    purchase_number: Optional[str] = Field(None, description="Purchase order number (auto-generated)")
    purchase_date: Optional[datetime] = Field(None, description="Date of purchase")
    delivery_date: Optional[datetime] = Field(None, description="Expected delivery date")
    total_amount: Optional[Decimal] = Field(None, ge=0, description="Total amount")
    tax_amount: Optional[Decimal] = Field(None, ge=0, description="Tax amount")
    status: Optional[PurchaseStatus] = Field(PurchaseStatus.PENDING, description="Purchase status")
    invoice_number: Optional[str] = Field(None, description="Invoice number")
    notes: Optional[str] = Field(None, description="Additional notes")

class PurchaseCreate(PurchaseBase):
    """Model for creating purchase orders"""
    items: List[PurchaseItemCreate] = Field(default_factory=list, description="Purchase items")

class PurchaseUpdate(BaseModel):
    """Model for updating purchase orders - includes all tracking fields"""
    # Basic fields
    supplier_id: Optional[UUID] = None
    purchase_number: Optional[str] = None
    purchase_date: Optional[datetime] = None
    delivery_date: Optional[datetime] = None
    total_amount: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    status: Optional[PurchaseStatus] = None
    invoice_number: Optional[str] = None
    notes: Optional[str] = None
    items: Optional[List[PurchaseItemCreate]] = None

    # Tracking fields
    confirmation_number: Optional[str] = Field(None, description="Supplier confirmation number")
    tracking_number: Optional[str] = Field(None, description="Shipping tracking number")
    carrier: Optional[str] = Field(None, description="Shipping carrier")
    estimated_delivery_date: Optional[datetime] = Field(None, description="Estimated delivery date")
    package_count: Optional[int] = Field(None, description="Number of packages")

    # State timestamps
    confirmed_at: Optional[datetime] = None
    preparing_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    invoiced_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    # Payment info
    payment_method: Optional[PaymentMethod] = None
    payment_reference: Optional[str] = Field(None, description="Payment transaction reference")

    # Additional metadata
    cancellation_reason: Optional[str] = None
    received_by: Optional[UUID] = None
    verified_by: Optional[UUID] = None
    package_condition: Optional[PackageCondition] = None

class Purchase(PurchaseBase):
    """Full purchase order model with all fields"""
    id: UUID
    tenant_id: UUID
    supplier_name: Optional[str] = Field(None, description="Supplier name (from JOIN)")
    created_by: Optional[UUID] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    items: List[PurchaseItem] = Field(default_factory=list)

    # Tracking fields
    confirmation_number: Optional[str] = None
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    estimated_delivery_date: Optional[datetime] = None
    package_count: Optional[int] = None

    # State timestamps
    confirmed_at: Optional[datetime] = None
    preparing_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    invoiced_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    # Payment info
    payment_method: Optional[PaymentMethod] = None
    payment_reference: Optional[str] = None

    # Additional metadata
    cancellation_reason: Optional[str] = None
    received_by: Optional[UUID] = None
    verified_by: Optional[UUID] = None
    package_condition: Optional[PackageCondition] = None

    class Config:
        from_attributes = True
        use_enum_values = True

class PurchaseWithDetails(Purchase):
    """Purchase with additional details (status history, attachments)"""
    status_history: List[PurchaseStatusHistory] = Field(default_factory=list)
    attachments: List[PurchaseAttachment] = Field(default_factory=list)

    # Calculated fields
    items_received_count: Optional[int] = None
    items_total_count: Optional[int] = None
    reception_percentage: Optional[float] = None

# =============================================================================
# RESPONSE MODELS
# =============================================================================

class PurchaseResponse(BaseModel):
    """Single purchase response"""
    success: bool = True
    data: Purchase

class PurchaseWithDetailsResponse(BaseModel):
    """Purchase with full details response"""
    success: bool = True
    data: PurchaseWithDetails

class PurchasesListResponse(BaseModel):
    """List of purchases response"""
    success: bool = True
    data: List[Purchase]
    total: int
    page: int = 1
    limit: int = 50

class StatusHistoryResponse(BaseModel):
    """Status history list response"""
    success: bool = True
    data: List[PurchaseStatusHistory]

class AttachmentsResponse(BaseModel):
    """Attachments list response"""
    success: bool = True
    data: List[PurchaseAttachment]

# =============================================================================
# STATE TRANSITION MODELS (for wizard forms)
# =============================================================================

class ConfirmPurchaseData(BaseModel):
    """Data for transitioning to confirmed state"""
    confirmation_number: str = Field(..., description="Supplier confirmation number")
    estimated_delivery_date: Optional[datetime] = Field(None, description="Estimated delivery date")
    notes: Optional[str] = None

class PreparingPurchaseData(BaseModel):
    """Data for transitioning to preparing state"""
    preparing_notes: Optional[str] = Field(None, description="Preparation notes")
    estimated_ship_date: Optional[datetime] = None

class ShipPurchaseData(BaseModel):
    """Data for transitioning to shipped state"""
    tracking_number: str = Field(..., description="Shipping tracking number")
    carrier: str = Field(..., description="Carrier name")
    estimated_delivery_date: Optional[datetime] = None
    package_count: Optional[int] = Field(None, gt=0)
    notes: Optional[str] = None

class ReceivePurchaseData(BaseModel):
    """Data for receiving items"""
    items: List[PurchaseItemUpdate] = Field(..., description="Items with received quantities")
    package_condition: PackageCondition
    reception_notes: Optional[str] = None
    partial: bool = Field(False, description="True if partial reception")

class VerifyPurchaseData(BaseModel):
    """Data for verifying received items"""
    items: List[PurchaseItemUpdate] = Field(..., description="Items with quality assessment")
    all_items_approved: bool = Field(..., description="All items meet specifications")
    verification_notes: Optional[str] = None

class InvoicePurchaseData(BaseModel):
    """Data for invoice registration"""
    invoice_number: str = Field(..., description="Supplier invoice number")
    invoice_date: datetime
    invoice_amount: Decimal = Field(..., gt=0)
    tax_amount: Decimal = Field(default=0, ge=0)
    payment_due_date: Optional[datetime] = None
    notes: Optional[str] = None

class PayPurchaseData(BaseModel):
    """Data for payment registration"""
    payment_method: PaymentMethod
    payment_reference: str = Field(..., description="Transaction reference")
    payment_amount: Decimal = Field(..., gt=0)
    payment_date: datetime
    notes: Optional[str] = None

class CancelPurchaseData(BaseModel):
    """Data for cancellation"""
    cancellation_reason: str = Field(..., min_length=10, description="Reason for cancellation")
    notes: Optional[str] = None
