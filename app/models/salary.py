from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import datetime
from typing import Optional, List
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
    payment_method: Optional[str] = Field(None, description="Payment method — UUID or slug (cash, card, digital, credit)")
    payment_reference: Optional[str] = Field(None, description="Payment reference number")
    payment_date: datetime = Field(..., description="Date of payment")
    notes: Optional[str] = Field(None, description="Additional notes")
    status: str = Field(default="paid", description="Payment status: pending, paid, cancelled")
    days_worked: Optional[int] = Field(None, ge=1, le=31, description="Days worked (for daily workers)")
    withholding_rate: Optional[Decimal] = Field(None, ge=0, le=1, description="Withholding rate 0–1 (e.g. 0.10 = 10%)")
    withholding_amount: Optional[Decimal] = Field(None, ge=0, description="Amount withheld for DIAN (account 2367)")
    ss_enabled: Optional[bool] = Field(False, description="Whether to apply SS/parafiscales deductions on this payment")
    arl_rate: Optional[Decimal] = Field(None, ge=0, le=Decimal('0.07'), description="ARL rate 0–0.07 (default 0.00522 = clase I)")
    caja_rate: Optional[Decimal] = Field(None, ge=0, le=Decimal('0.04'), description="Caja Compensación rate (default 0.04)")


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
    net_pay: Optional[Decimal] = None
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
    label: Optional[str] = Field(None, description="Purpose tag, e.g. 'pila' for PILA/social security proof")


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
    arl_rate: Optional[Decimal] = None

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


# =============================================================================
# PRIMA DE SERVICIOS MODELS
# =============================================================================

class PrimaPaymentCreate(BaseModel):
    semestre: str  # '2025-S1' or '2025-S2'
    gross_salary: Decimal
    days_worked: Optional[int] = 180  # days in the semester (180 = full semester)
    payment_method: Optional[str] = None
    payment_date: Optional[datetime] = None
    notes: Optional[str] = None

    @field_validator('semestre')
    @classmethod
    def validate_semestre(cls, v: str) -> str:
        import re
        if not re.match(r'^\d{4}-S[12]$', v):
            raise ValueError("semestre must be in format 'YYYY-S1' or 'YYYY-S2'")
        return v


class PrimaPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    semestre: str
    gross_salary: Decimal
    days_worked: int
    prima_amount: Decimal
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class PrimaPaymentListResponse(BaseModel):
    success: bool = True
    data: List[PrimaPayment]


class PrimaPaymentResponse(BaseModel):
    success: bool = True
    data: PrimaPayment


# =============================================================================
# CESANTÍAS MODELS
# =============================================================================

class CesantiasPaymentCreate(BaseModel):
    anio: int
    gross_salary: Decimal
    days_worked: Optional[int] = 360
    fondo_name: Optional[str] = None
    payment_method: Optional[str] = None
    payment_date: Optional[datetime] = None
    notes: Optional[str] = None

    @field_validator('anio')
    @classmethod
    def validate_anio(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError("anio must be a valid year (2000–2100)")
        return v


class CesantiasPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    anio: int
    gross_salary: Decimal
    days_worked: int
    cesantias_amount: Decimal
    fondo_name: Optional[str] = None
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class CesantiasPaymentListResponse(BaseModel):
    success: bool = True
    data: List[CesantiasPayment]


class CesantiasPaymentResponse(BaseModel):
    success: bool = True
    data: CesantiasPayment


# =============================================================================
# INTERESES SOBRE CESANTÍAS MODELS
# =============================================================================

class IntCesantiasPaymentCreate(BaseModel):
    anio: int
    cesantias_base: Decimal
    int_cesantias_amount: Optional[Decimal] = None  # if None, calculate as base * 0.12
    payment_method: Optional[str] = None
    payment_date: Optional[datetime] = None
    notes: Optional[str] = None

    @field_validator('anio')
    @classmethod
    def validate_anio(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError("anio must be a valid year (2000–2100)")
        return v


class IntCesantiasPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    anio: int
    cesantias_base: Decimal
    int_cesantias_amount: Decimal
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class IntCesantiasPaymentListResponse(BaseModel):
    success: bool = True
    data: List[IntCesantiasPayment]


class IntCesantiasPaymentResponse(BaseModel):
    success: bool = True
    data: IntCesantiasPayment


# =============================================================================
# VACACIONES MODELS
# =============================================================================

class VacacionesPaymentCreate(BaseModel):
    anio: int
    gross_salary: Decimal
    days_worked: Optional[int] = 360
    payment_method: Optional[str] = None
    payment_date: Optional[datetime] = None
    notes: Optional[str] = None

    @field_validator('anio')
    @classmethod
    def validate_anio(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError("anio must be a valid year (2000–2100)")
        return v


class VacacionesPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    anio: int
    gross_salary: Decimal
    days_worked: int
    vacaciones_amount: Decimal
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class VacacionesPaymentListResponse(BaseModel):
    success: bool = True
    data: List[VacacionesPayment]


class VacacionesPaymentResponse(BaseModel):
    success: bool = True
    data: VacacionesPayment


# =============================================================================
# DOTACIÓN MODELS
# =============================================================================

class DotacionPaymentCreate(BaseModel):
    period: str = Field(..., description="Period slot: APR, AUG, or DEC")
    year: int = Field(..., description="Year (2000–2100)")
    items_description: Optional[str] = Field(None, description="Description of items delivered")
    total_amount: Decimal = Field(..., gt=0, description="Total value of dotación delivered")
    payment_method: Optional[str] = Field(None, description="Payment method — UUID or slug")
    payment_date: Optional[datetime] = Field(None, description="Date of delivery/payment")
    notes: Optional[str] = Field(None, description="Additional notes")

    @field_validator('period')
    @classmethod
    def validate_period(cls, v: str) -> str:
        if v not in ('APR', 'AUG', 'DEC'):
            raise ValueError("period must be 'APR', 'AUG', or 'DEC'")
        return v

    @field_validator('year')
    @classmethod
    def validate_year(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError("year must be between 2000 and 2100")
        return v


class DotacionPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    period: str
    year: int
    items_description: Optional[str] = None
    total_amount: Decimal
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class DotacionPaymentListResponse(BaseModel):
    success: bool = True
    data: List[DotacionPayment]


class DotacionPaymentResponse(BaseModel):
    success: bool = True
    data: DotacionPayment


# =============================================================================
# PILA PAYMENT MODELS
# =============================================================================

class PilaPaymentCreate(BaseModel):
    period_month: str = Field(..., description="Period in YYYY-MM format")
    employee_ss_amount: Decimal = Field(default=Decimal('0'), ge=0, description="Employee SS amount (237005)")
    employer_ss_amount: Decimal = Field(default=Decimal('0'), ge=0, description="Employer SS amount (237010)")
    total_amount: Decimal = Field(..., gt=0, description="Total PILA disbursement amount")
    payment_method: Optional[str] = Field(None, description="Payment method — UUID or slug")
    payment_date: Optional[datetime] = Field(None, description="Date of PILA payment")
    notes: Optional[str] = Field(None, description="Additional notes")

    @field_validator('period_month')
    @classmethod
    def validate_period_month(cls, v: str) -> str:
        import re
        if not re.match(r'^\d{4}-\d{2}$', v):
            raise ValueError("period_month must be in YYYY-MM format")
        return v


class PilaPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    period_month: str
    employee_ss_amount: Decimal
    employer_ss_amount: Decimal
    total_amount: Decimal
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class PilaPaymentListResponse(BaseModel):
    success: bool = True
    data: List[PilaPayment]


class PilaPaymentResponse(BaseModel):
    success: bool = True
    data: PilaPayment


class PilaPendingByPeriod(BaseModel):
    period_month: str
    employee_ss_pending: Decimal
    employer_ss_pending: Decimal
    total_pending: Decimal


class PilaPendingResponse(BaseModel):
    success: bool = True
    data: List[PilaPendingByPeriod]


# =============================================================================
# OVERTIME (HORAS EXTRAS) PAYMENT MODELS
# =============================================================================

class OvertimePaymentCreate(BaseModel):
    period_month: str = Field(..., description="Period in YYYY-MM format")
    hours_diurna: Decimal = Field(default=Decimal('0'), ge=0, description="Overtime diurna hours (+25%)")
    hours_nocturna: Decimal = Field(default=Decimal('0'), ge=0, description="Overtime nocturna hours (+75%)")
    hours_dominical_diurna: Decimal = Field(default=Decimal('0'), ge=0, description="Dominical/festivo diurna hours (+100%)")
    hours_dominical_nocturna: Decimal = Field(default=Decimal('0'), ge=0, description="Dominical/festivo nocturna hours (+150%)")
    base_hourly_rate: Decimal = Field(..., gt=0, description="Base hourly rate used for this payment")
    total_amount: Decimal = Field(..., gt=0, description="Total overtime amount (pre-calculated by client)")
    payment_method: Optional[str] = Field(None, description="Payment method — UUID or slug")
    payment_date: Optional[datetime] = Field(None, description="Date of payment")
    notes: Optional[str] = Field(None, description="Additional notes")

    @field_validator('period_month')
    @classmethod
    def validate_period_month(cls, v: str) -> str:
        import re
        if not re.match(r'^\d{4}-\d{2}$', v):
            raise ValueError("period_month must be in YYYY-MM format")
        return v

    from pydantic import model_validator

    @model_validator(mode='after')
    def check_at_least_one_hour(self) -> 'OvertimePaymentCreate':
        total_hours = (
            self.hours_diurna + self.hours_nocturna
            + self.hours_dominical_diurna + self.hours_dominical_nocturna
        )
        if total_hours <= 0:
            raise ValueError("At least one hours field must be greater than zero")
        return self


class OvertimePayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    tenant_member_id: UUID
    period_month: str
    hours_diurna: Decimal
    hours_nocturna: Decimal
    hours_dominical_diurna: Decimal
    hours_dominical_nocturna: Decimal
    base_hourly_rate: Decimal
    total_amount: Decimal
    payment_method: Optional[str] = None
    payment_date: datetime
    notes: Optional[str] = None
    created_at: datetime


class OvertimePaymentListResponse(BaseModel):
    success: bool = True
    data: List[OvertimePayment]


class OvertimePaymentResponse(BaseModel):
    success: bool = True
    data: OvertimePayment
