from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


# --- Account Templates (system PUC) ---

class AccountTemplate(BaseModel):
    id: UUID
    code: str
    standard_name: str = Field(alias='standardName')
    account_class: str = Field(alias='accountClass')
    account_type: str = Field(alias='accountType')
    normal_balance: str = Field(alias='normalBalance')
    level: int
    parent_code: Optional[str] = Field(None, alias='parentCode')
    is_detail: bool = Field(alias='isDetail')
    niif_group: Optional[str] = Field(None, alias='niifGroup')
    is_active: bool = Field(alias='isActive')

    class Config:
        populate_by_name = True


# --- Tenant Accounts ---

class TenantAccountBase(BaseModel):
    code: str
    name: str
    account_class: str = Field(alias='accountClass')
    account_type: str = Field(alias='accountType')
    normal_balance: str = Field(alias='normalBalance')
    level: int
    parent_id: Optional[UUID] = Field(None, alias='parentId')
    is_detail: bool = Field(default=False, alias='isDetail')
    is_active: bool = Field(default=True, alias='isActive')

    class Config:
        populate_by_name = True


class TenantAccountCreate(TenantAccountBase):
    template_id: Optional[UUID] = Field(None, alias='templateId')


class TenantAccountUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = Field(None, alias='isActive')
    is_detail: Optional[bool] = Field(None, alias='isDetail')

    class Config:
        populate_by_name = True


class TenantAccount(TenantAccountBase):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    template_id: Optional[UUID] = Field(None, alias='templateId')
    is_system: bool = Field(alias='isSystem')
    created_at: datetime = Field(alias='createdAt')

    class Config:
        populate_by_name = True


# --- Journal Lines ---

class JournalLineCreate(BaseModel):
    account_id: UUID = Field(alias='accountId')
    debit: float = 0
    credit: float = 0
    description: Optional[str] = None
    line_order: int = Field(default=0, alias='lineOrder')

    class Config:
        populate_by_name = True


class JournalLine(JournalLineCreate):
    id: UUID
    journal_entry_id: UUID = Field(alias='journalEntryId')
    created_at: datetime = Field(alias='createdAt')

    class Config:
        populate_by_name = True


# --- Journal Entries ---

class JournalEntryCreate(BaseModel):
    entry_date: str = Field(alias='entryDate')  # YYYY-MM-DD
    description: str
    reference: Optional[str] = None
    lines: List[JournalLineCreate]
    # Issue #531 — annotation flag for asientos created via "Actualizar saldo
    # real" with motivo "no estoy seguro". When true, the asiento is fully
    # posted and balanced — only marks it for accountant review later.
    source_module: Optional[str] = Field(None, alias='sourceModule')
    source_id: Optional[UUID] = Field(None, alias='sourceId')
    pending_review: Optional[bool] = Field(False, alias='pendingReview')

    class Config:
        populate_by_name = True


class JournalEntry(BaseModel):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    entry_date: str = Field(alias='entryDate')
    period_year: int = Field(alias='periodYear')
    period_month: int = Field(alias='periodMonth')
    description: str
    reference: Optional[str] = None
    source_module: Optional[str] = Field(None, alias='sourceModule')
    source_id: Optional[UUID] = Field(None, alias='sourceId')
    status: str
    total_debit: float = Field(alias='totalDebit')
    total_credit: float = Field(alias='totalCredit')
    created_by: Optional[UUID] = Field(None, alias='createdBy')
    posted_at: Optional[datetime] = Field(None, alias='postedAt')
    voided_at: Optional[datetime] = Field(None, alias='voidedAt')
    created_at: datetime = Field(alias='createdAt')
    pending_review: bool = Field(False, alias='pendingReview')

    class Config:
        populate_by_name = True


class JournalEntryWithLines(JournalEntry):
    lines: List[JournalLine] = []

    class Config:
        populate_by_name = True


# --- Responses ---

class TenantAccountResponse(BaseModel):
    success: bool = True
    data: TenantAccount

    class Config:
        populate_by_name = True


class TenantAccountsListResponse(BaseModel):
    success: bool = True
    data: List[TenantAccount]

    class Config:
        populate_by_name = True


class JournalEntryResponse(BaseModel):
    success: bool = True
    data: JournalEntryWithLines

    class Config:
        populate_by_name = True


class JournalEntriesListResponse(BaseModel):
    success: bool = True
    data: List[JournalEntry]
    total: int
    opening_balance: Optional[float] = Field(None, alias='openingBalance')

    class Config:
        populate_by_name = True


class AccountTemplatesListResponse(BaseModel):
    success: bool = True
    data: List[AccountTemplate]

    class Config:
        populate_by_name = True


class JournalEntryVoidRequest(BaseModel):
    reason: str


# --- Trial Balance (#379) ---

class TrialBalanceRow(BaseModel):
    account_id: str = Field(alias='accountId')
    code: str
    name: str
    account_class: str = Field(alias='class')
    account_type: str  = Field(alias='accountType')
    normal_balance: str = Field(alias='normalBalance')
    opening_balance: float = Field(alias='openingBalance')
    period_debits: float   = Field(alias='periodDebits')
    period_credits: float  = Field(alias='periodCredits')
    closing_balance: float = Field(alias='closingBalance')

    class Config:
        populate_by_name = True


class TrialBalanceResponse(BaseModel):
    success: bool = True
    period_start: str  = Field(alias='periodStart')
    period_end: str    = Field(alias='periodEnd')
    rows: List[TrialBalanceRow]
    total_debits: float  = Field(alias='totalDebits')
    total_credits: float = Field(alias='totalCredits')
    is_balanced: bool    = Field(alias='isBalanced')

    class Config:
        populate_by_name = True


# --- P&L Statement (#383) ---

class PLRevenue(BaseModel):
    food_beverage_sales: float = Field(alias='foodBeverageSales')
    total: float

    class Config:
        populate_by_name = True


class PLCogs(BaseModel):
    food_cost: float = Field(alias='foodCost')
    total: float

    class Config:
        populate_by_name = True


class PLOperatingExpenses(BaseModel):
    payroll: float
    rent: float
    utilities: float
    maintenance: float
    other: float
    total: float

    class Config:
        populate_by_name = True


class PLProvisions(BaseModel):
    cesantias: float
    prima: float
    vacaciones: float
    intereses_cesantias: float = Field(alias='interesesCesantias')
    total: float

    class Config:
        populate_by_name = True


class PLPrimeCost(BaseModel):
    food_cost_pct: float = Field(alias='foodCostPct')
    labor_pct: float = Field(alias='laborPct')
    total_pct: float = Field(alias='totalPct')
    benchmark_pct: float = Field(default=65.0, alias='benchmarkPct')
    status: str  # 'ok' | 'warning'

    class Config:
        populate_by_name = True


class PLPeriodData(BaseModel):
    period: str  # 'YYYY-MM'
    revenue: PLRevenue
    cogs: PLCogs
    gross_profit: float = Field(alias='grossProfit')
    gross_margin_pct: float = Field(alias='grossMarginPct')
    operating_expenses: PLOperatingExpenses = Field(alias='operatingExpenses')
    ebitda: float
    ebitda_margin_pct: float = Field(alias='ebitdaMarginPct')
    provisions: PLProvisions
    net_income: float = Field(alias='netIncome')
    prime_cost: PLPrimeCost = Field(alias='primeCost')

    class Config:
        populate_by_name = True


class PLStatementResponse(BaseModel):
    success: bool = True
    current: PLPeriodData
    previous: Optional[PLPeriodData] = None

    class Config:
        populate_by_name = True


# --- Provisions (#384) ---

class ProvisionsBreakdown(BaseModel):
    cesantias: float
    intereses_cesantias: float = Field(alias='interesesCesantias')
    prima: float
    vacaciones: float
    total: float

    class Config:
        populate_by_name = True


class ProvisionsPreviewResponse(BaseModel):
    success: bool = True
    period: str  # 'YYYY-MM'
    payroll_base: float = Field(alias='payrollBase')
    transport_base: float = Field(alias='transportBase')
    vacation_base: float = Field(alias='vacationBase')
    employee_count: int = Field(alias='employeeCount')
    provisions: ProvisionsBreakdown

    class Config:
        populate_by_name = True


class ProvisionsPostResponse(BaseModel):
    success: bool = True
    period: str  # 'YYYY-MM'
    provisions: ProvisionsBreakdown
    journal_entry_ids: List[str] = Field(alias='journalEntryIds')
    voided_count: int = Field(alias='voidedCount')

    class Config:
        populate_by_name = True
