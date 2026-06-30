"""
Cierre Contable — Pydantic models
Issue: https://github.com/uno0uno/warocol.com/issues/311
"""
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field
from datetime import date, datetime
from uuid import UUID


class OpenShiftCreate(BaseModel):
    period_start: date = Field(alias='periodStart')
    period_end: date = Field(alias='periodEnd')
    period_start_time: Optional[datetime] = Field(None, alias='periodStartTime')
    period_end_time: Optional[datetime] = Field(None, alias='periodEndTime')
    shift_template_id: Optional[UUID] = Field(None, alias='shiftTemplateId')
    opening_cash: float = Field(alias='openingCash', ge=0)
    opening_breakdown: Optional[Dict[str, Any]] = Field(None, alias='openingBreakdown')

    class Config:
        populate_by_name = True


class CierrePaymentBreakdownReported(BaseModel):
    group_slug: str = Field(alias='groupSlug', min_length=1)
    method_name: str = Field(alias='methodName', min_length=1)
    reported_amount: float = Field(alias='reportedAmount')

    class Config:
        populate_by_name = True


class CierreCreate(BaseModel):
    period_start: date = Field(alias='periodStart')
    period_end: date = Field(alias='periodEnd')
    # Optional exact timestamps — when provided, order filtering uses these
    # instead of the date-truncated comparison (supports cross-midnight shifts).
    period_start_time: Optional[datetime] = Field(None, alias='periodStartTime')
    period_end_time: Optional[datetime] = Field(None, alias='periodEndTime')
    shift_template_id: Optional[UUID] = Field(None, alias='shiftTemplateId')
    cash_counted: float = Field(alias='cashCounted')
    cash_left_in_drawer: Optional[float] = Field(None, alias='cashLeftInDrawer', ge=0)
    payment_breakdown_reported: Optional[List[CierrePaymentBreakdownReported]] = Field(None, alias='paymentBreakdownReported')
    notes: Optional[str] = None

    class Config:
        populate_by_name = True


class CierreReconciliationReportedUpdate(BaseModel):
    reported_amount: float = Field(alias='reportedAmount')
    notes: Optional[str] = None

    class Config:
        populate_by_name = True


class CierreReconciliationResolve(BaseModel):
    reason: str
    notes: Optional[str] = None
    create_journal_entry: bool = Field(False, alias='createJournalEntry')

    class Config:
        populate_by_name = True


class CierreCashSettingsUpdate(BaseModel):
    default_opening_cash: Optional[float] = Field(None, alias='defaultOpeningCash', ge=0)

    class Config:
        populate_by_name = True


class CierrePreviewData(BaseModel):
    total_sales: float = Field(alias='totalSales')
    items_sold: int = Field(alias='itemsSold')
    total_cash: float = Field(alias='totalCash')
    total_card: float = Field(alias='totalCard')
    total_digital: float = Field(alias='totalDigital')
    total_credit: float = Field(alias='totalCredit')
    gastos_efectivo: float = Field(alias='gastosEfectivo')
    cash_purchases: float = Field(0, alias='cashPurchases')
    opening_cash: float = Field(0, alias='openingCash')
    cash_expected: float = Field(alias='cashExpected')
    open_tables_count: int = Field(alias='openTablesCount')

    class Config:
        populate_by_name = True


class CierrePreviewResponse(BaseModel):
    success: bool = True
    data: CierrePreviewData


class ClosingSummaryOut(BaseModel):
    id: UUID
    accounting_period_id: UUID = Field(alias='accountingPeriodId')
    tenant_id: UUID = Field(alias='tenantId')
    period_start: date = Field(alias='periodStart')
    period_end: date = Field(alias='periodEnd')
    total_sales: float = Field(alias='totalSales')
    items_sold: int = Field(alias='itemsSold')
    total_cash: float = Field(alias='totalCash')
    total_card: float = Field(alias='totalCard')
    total_digital: float = Field(alias='totalDigital')
    total_credit: float = Field(alias='totalCredit')
    gastos_efectivo: float = Field(alias='gastosEfectivo')
    cash_purchases: float = Field(0, alias='cashPurchases')
    opening_cash: float = Field(0, alias='openingCash')
    cash_left_in_drawer: Optional[float] = Field(None, alias='cashLeftInDrawer')
    cash_expected: float = Field(alias='cashExpected')
    cash_counted: float = Field(alias='cashCounted')
    cash_difference: float = Field(alias='cashDifference')
    notes: Optional[str] = None
    closed_at: datetime = Field(alias='closedAt')

    class Config:
        populate_by_name = True


class CierreDetailResponse(BaseModel):
    success: bool = True
    data: ClosingSummaryOut


class CierreListResponse(BaseModel):
    success: bool = True
    data: List[ClosingSummaryOut]


# ---------------------------------------------------------------------------
# Monthly Accounting Period — #362
# ---------------------------------------------------------------------------

class MonthlyPeriodClose(BaseModel):
    notes: Optional[str] = None

    class Config:
        populate_by_name = True


class MonthlyPeriod(BaseModel):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    year: int
    month: int
    status: str  # 'open' | 'closed'
    closed_by: Optional[UUID] = Field(None, alias='closedBy')
    closed_at: Optional[datetime] = Field(None, alias='closedAt')
    notes: Optional[str] = None
    created_at: datetime = Field(alias='createdAt')

    class Config:
        populate_by_name = True


class MonthlyPeriodResponse(BaseModel):
    success: bool = True
    data: MonthlyPeriod

    class Config:
        populate_by_name = True
