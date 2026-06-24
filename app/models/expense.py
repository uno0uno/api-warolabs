from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from uuid import UUID
from enum import Enum

class ExpenseType(str, Enum):
    """Expense classification type — aligned with NIIF/IFRS and LATAM standards"""
    COGS = "cogs"                           # Costo de ventas (compras directas, ingredientes)
    ADMIN_EXPENSE = "admin_expense"         # Gasto administrativo
    SALES_EXPENSE = "sales_expense"         # Gasto de ventas
    FINANCIAL_EXPENSE = "financial_expense" # Gasto financiero (intereses, comisiones, fees)
    OTHER_EXPENSE = "other_expense"         # Otro gasto no operacional

    class Config:
        use_enum_values = True

class RecurrenceFrequency(str, Enum):
    """Expense recurrence frequency options"""
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"

    class Config:
        use_enum_values = True

class ChangeType(str, Enum):
    """Types of changes tracked in expense history"""
    FIELD_UPDATE = "field_update"
    CREATED = "created"
    DELETED = "deleted"
    ATTACHMENT_ADDED = "attachment_added"
    ATTACHMENT_REMOVED = "attachment_removed"

    class Config:
        use_enum_values = True

class InstanceStatus(str, Enum):
    """Status of recurring expense payment instances"""
    PENDING = "pending"
    PAID = "paid"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    class Config:
        use_enum_values = True

class ExpenseCategory(BaseModel):
    id: UUID
    category_code: str = Field(alias='categoryCode')
    category_name: str = Field(alias='categoryName')
    description: Optional[str] = None
    is_active: bool = Field(default=True, alias='isActive')

    class Config:
        populate_by_name = True

class ExpenseBase(BaseModel):
    expense_category_id: UUID = Field(alias='expenseCategoryId')
    amount: float
    description: Optional[str] = None
    transaction_date: date = Field(alias='transactionDate')
    is_recurring: bool = Field(default=False, alias='isRecurring')
    frequency: Optional[RecurrenceFrequency] = None
    recurring_end_date: Optional[date] = Field(None, alias='recurringEndDate')
    payment_method: Optional[str] = Field('cash', alias='paymentMethod')
    payment_method_id: Optional[str] = Field(None, alias='paymentMethodId')
    expense_type: Optional[ExpenseType] = Field(None, alias='expenseType')

    class Config:
        populate_by_name = True
        use_enum_values = True

class ExpenseCreate(ExpenseBase):
    pass

class ExpenseUpdate(BaseModel):
    expense_category_id: Optional[UUID] = Field(None, alias='expenseCategoryId')
    amount: Optional[float] = None
    description: Optional[str] = None
    transaction_date: Optional[date] = Field(None, alias='transactionDate')
    is_recurring: Optional[bool] = Field(None, alias='isRecurring')
    frequency: Optional[RecurrenceFrequency] = None
    recurring_end_date: Optional[date] = Field(None, alias='recurringEndDate')
    payment_method: Optional[str] = Field(None, alias='paymentMethod')
    payment_method_id: Optional[str] = Field(None, alias='paymentMethodId')
    expense_type: Optional[ExpenseType] = Field(None, alias='expenseType')

    class Config:
        populate_by_name = True
        use_enum_values = True

class Expense(ExpenseBase):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    month_year: str = Field(alias='monthYear')
    source_system: Optional[str] = Field(None, alias='sourceSystem')
    expense_number: Optional[str] = Field(None, alias='expenseNumber')
    created_at: datetime = Field(alias='createdAt')
    category: Optional[ExpenseCategory] = None
    attachments: Optional[List[Dict[str, Any]]] = Field(default_factory=list)

    class Config:
        populate_by_name = True
        use_enum_values = True

class ExpensesStats(BaseModel):
    total_amount: float = Field(default=0, alias='totalAmount')
    count: int = Field(default=0)
    by_category: Dict[str, float] = Field(default_factory=dict, alias='byCategory')

    class Config:
        populate_by_name = True

class ExpenseChangeHistoryBase(BaseModel):
    expense_id: UUID = Field(alias='expenseId')
    change_type: ChangeType = Field(alias='changeType')
    field_changed: Optional[str] = Field(None, alias='fieldChanged')
    old_value: Optional[Dict[str, Any]] = Field(None, alias='oldValue')
    new_value: Optional[Dict[str, Any]] = Field(None, alias='newValue')
    expense_snapshot: Optional[Dict[str, Any]] = Field(None, alias='expenseSnapshot')
    notes: Optional[str] = None

    class Config:
        populate_by_name = True
        use_enum_values = True

class ExpenseChangeHistory(ExpenseChangeHistoryBase):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    changed_by: Optional[UUID] = Field(None, alias='changedBy')
    changed_at: datetime = Field(alias='changedAt')
    created_at: datetime = Field(alias='createdAt')

    class Config:
        populate_by_name = True
        use_enum_values = True
        json_encoders = {datetime: lambda v: v.isoformat()}

class RecurringExpenseInstanceBase(BaseModel):
    expense_id: UUID = Field(alias='expenseId')
    period_month: str = Field(alias='periodMonth')
    scheduled_date: date = Field(alias='scheduledDate')
    amount: float
    status: InstanceStatus = InstanceStatus.PENDING
    payment_date: Optional[datetime] = Field(None, alias='paymentDate')
    payment_method: Optional[str] = Field(None, alias='paymentMethod')
    payment_reference: Optional[str] = Field(None, alias='paymentReference')
    notes: Optional[str] = None

    class Config:
        populate_by_name = True
        use_enum_values = True

class RecurringExpenseInstance(RecurringExpenseInstanceBase):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    created_by: Optional[UUID] = Field(None, alias='createdBy')
    created_at: datetime = Field(alias='createdAt')
    updated_at: datetime = Field(alias='updatedAt')
    attachments: Optional[List[Dict[str, Any]]] = Field(default_factory=list)

    class Config:
        populate_by_name = True
        use_enum_values = True
        json_encoders = {datetime: lambda v: v.isoformat(), date: lambda v: v.isoformat()}

class RecurringExpenseInstanceCreate(BaseModel):
    period_month: str = Field(alias='periodMonth')
    scheduled_date: date = Field(alias='scheduledDate')
    amount: Optional[float] = None
    status: str = 'pending'
    payment_date: Optional[str] = Field(None, alias='paymentDate')
    payment_method: Optional[str] = Field(None, alias='paymentMethod')
    payment_reference: Optional[str] = Field(None, alias='paymentReference')
    notes: Optional[str] = None

    class Config:
        populate_by_name = True

class RecurringExpenseInstanceUpdate(BaseModel):
    status: Optional[str] = None
    payment_date: Optional[str] = Field(None, alias='paymentDate')
    payment_method: Optional[str] = Field(None, alias='paymentMethod')
    payment_reference: Optional[str] = Field(None, alias='paymentReference')
    notes: Optional[str] = None

    class Config:
        populate_by_name = True

class ExpensesListResponse(BaseModel):
    success: bool = True
    data: List[Expense]
    stats: Optional[ExpensesStats] = None
    page: int
    limit: int
    total: int

    class Config:
        populate_by_name = True

class ExpenseResponse(BaseModel):
    success: bool = True
    data: Expense

    class Config:
        populate_by_name = True

class ExpenseCategoriesResponse(BaseModel):
    success: bool = True
    data: List[ExpenseCategory]

    class Config:
        populate_by_name = True
