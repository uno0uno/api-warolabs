from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from typing import Optional, List, Literal
from uuid import UUID
from decimal import Decimal
from enum import Enum


# =============================================================================
# ENUMS
# =============================================================================

class SalaryType(str, Enum):
    """Salary type options"""
    SMMLV = "smmlv"
    FIXED = "fixed"
    HOURLY = "hourly"


class EmploymentType(str, Enum):
    """Employment contract type"""
    EMPLOYEE = "employee"       # Contrato laboral — DR 5105 Sueldos
    CONTRACTOR = "contractor"   # Prestación de servicios — DR 5199 Honorarios
    DAILY = "daily"             # Jornalero — pago por días × tarifa diaria


class PaymentMethod(str, Enum):
    """Payment method types"""
    TRANSFER = "transfer"
    CASH = "cash"
    CHECK = "check"
    OTHER = "other"


class PaymentFrequency(str, Enum):
    """Payment frequency types"""
    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"
    WEEKLY = "weekly"


# =============================================================================
# EMPLOYEE SALARY CONFIG MODELS
# =============================================================================

class EmployeeSalaryConfigBase(BaseModel):
    """Base model for employee salary configuration"""
    salary_type: SalaryType = Field(default=SalaryType.SMMLV, description="Type of salary: smmlv, fixed, or hourly")
    minimum_wage_multiplier: Optional[Decimal] = Field(None, ge=0.5, le=10, description="Multiplier for SMMLV")
    fixed_amount: Optional[Decimal] = Field(None, ge=0, description="Fixed monthly amount")
    hourly_rate: Optional[Decimal] = Field(None, ge=0, description="Hourly rate amount")
    payment_frequency: PaymentFrequency = Field(default=PaymentFrequency.MONTHLY, description="Payment frequency")
    notes: Optional[str] = Field(None, description="Additional notes")
    employment_type: Optional[EmploymentType] = Field(None, description="Employment contract type")
    daily_rate: Optional[Decimal] = Field(None, ge=0, description="Daily rate for jornalero workers")


class EmployeeSalaryConfigCreate(EmployeeSalaryConfigBase):
    """Model for creating/updating employee salary config"""
    pass


class EmployeeSalaryConfig(EmployeeSalaryConfigBase):
    """Full employee salary config model"""
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: UUID
    tenant_member_id: UUID
    period_month: str
    base_salary: Decimal
    total_salary: Decimal
    calculated_salary: Optional[Decimal] = Field(None, description="Calculated salary based on type")
    created_at: Optional[datetime] = None


# =============================================================================
# SALARY PAYMENT MODELS
# =============================================================================

class SalaryPaymentBase(BaseModel):
    """Base model for salary payments"""
    period_month: str = Field(..., description="Payment period in YYYY-MM format")
    payment_amount: Decimal = Field(..., gt=0, description="Amount paid")
    payment_method: Optional[PaymentMethod] = Field(None, description="Payment method")
    payment_reference: Optional[str] = Field(None, description="Payment reference number")
    payment_date: datetime = Field(..., description="Date of payment")
    notes: Optional[str] = Field(None, description="Additional notes")
    status: str = Field(default="paid", description="Payment status: pending, paid, cancelled")
    days_worked: Optional[int] = Field(None, ge=1, le=31, description="Days worked (for daily workers)")


class SalaryPaymentCreate(SalaryPaymentBase):
    """Model for creating salary payments"""
    tenant_member_id: UUID = Field(..., description="Employee tenant member ID")


class SalaryPaymentUpdate(BaseModel):
    """Model for updating salary payments"""
    payment_amount: Optional[Decimal] = Field(None, gt=0)
    payment_method: Optional[PaymentMethod] = None
    payment_reference: Optional[str] = None
    payment_date: Optional[datetime] = None
    notes: Optional[str] = None


class SalaryPayment(SalaryPaymentBase):
    """Full salary payment model"""
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    days_worked: Optional[int] = None
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime
    attachments: List["SalaryAttachment"] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


# =============================================================================
# SALARY ATTACHMENT MODELS
# =============================================================================

class SalaryAttachmentBase(BaseModel):
    """Base model for salary attachments"""
    path: str = Field(..., description="File path/URL in storage")
    file_name: str = Field(..., description="Original file name")
    file_size: Optional[int] = Field(None, description="File size in bytes")
    mime_type: Optional[str] = Field(None, description="MIME type")
    s3_key: Optional[str] = Field(None, description="S3 key for the file")


class SalaryAttachmentCreate(SalaryAttachmentBase):
    """Model for creating salary attachments"""
    salary_payment_id: UUID
    tenant_id: UUID
    uploaded_by: UUID


class SalaryAttachment(SalaryAttachmentBase):
    """Full salary attachment model"""
    id: UUID
    tenant_id: UUID
    salary_payment_id: UUID
    uploaded_by: Optional[UUID] = None
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# EMPLOYEE WITH SALARY MODELS
# =============================================================================

class EmployeeWithSalary(BaseModel):
    """Employee with salary configuration"""
    id: UUID = Field(..., description="Tenant member ID")
    user_id: Optional[UUID] = None
    name: str
    email: Optional[str] = None
    role: str
    role_label: str
    initials: str
    color: str

    # Salary config
    salary_type: Optional[str] = None
    multiplier: Optional[Decimal] = None
    fixed_amount: Optional[Decimal] = None
    hourly_rate: Optional[Decimal] = None
    payment_frequency: Optional[str] = None
    calculated_salary: Optional[Decimal] = None
    salary_notes: Optional[str] = None
    employment_type: Optional[str] = None
    daily_rate: Optional[Decimal] = None

    # Last payment info
    last_payment_date: Optional[datetime] = None
    last_payment_amount: Optional[Decimal] = None
    last_payment_period: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EmployeeDetailWithPayments(EmployeeWithSalary):
    """Employee with full payment history"""
    payments: List[SalaryPayment] = Field(default_factory=list)
    total_paid_this_year: Decimal = Field(default=0)
    payments_count: int = Field(default=0)


# =============================================================================
# RESPONSE MODELS
# =============================================================================

class EmployeesWithSalaryResponse(BaseModel):
    """List of employees with salary info"""
    success: bool = True
    data: List[EmployeeWithSalary]
    smmlv: Decimal = Field(..., description="Current SMMLV value")


class EmployeeDetailResponse(BaseModel):
    """Single employee detail response"""
    success: bool = True
    data: EmployeeDetailWithPayments
    smmlv: Decimal


class SalaryConfigResponse(BaseModel):
    """Salary config update response"""
    success: bool = True
    data: EmployeeSalaryConfig


class SalaryPaymentResponse(BaseModel):
    """Single payment response"""
    success: bool = True
    data: SalaryPayment


class SalaryPaymentsListResponse(BaseModel):
    """List of payments response"""
    success: bool = True
    data: List[SalaryPayment]
    total: int


class PayrollSummaryResponse(BaseModel):
    """Monthly payroll summary"""
    success: bool = True
    period_month: str
    total_employees: int
    employees_paid: int
    total_payroll: Decimal
    total_paid: Decimal
    pending_amount: Decimal


# Forward reference update
SalaryPayment.model_rebuild()
