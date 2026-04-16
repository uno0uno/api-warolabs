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

    class Config:
        populate_by_name = True


class AccountTemplatesListResponse(BaseModel):
    success: bool = True
    data: List[AccountTemplate]

    class Config:
        populate_by_name = True
