"""
Cierre Contable Router
Daily accounting close: preview (Cierre X) and final close wizard (Cierre Z).

Issue: https://github.com/uno0uno/warocol.com/issues/311
"""
from fastapi import Depends, APIRouter, Request, Response, Query
from app.core.permissions import Module, require_module
from typing import Optional
from uuid import UUID
from datetime import date, datetime
from app.models.cierre import (
    CierreCashSettingsUpdate,
    CierreCreate,
    CierreReconciliationReportedUpdate,
    CierreReconciliationResolve,
    MonthlyPeriodClose,
    OpenShiftCreate,
)
from app.services import cierre_service

router = APIRouter(prefix="/cierre", tags=["cierre"])


@router.get("/preview", dependencies=[Depends(require_module(Module.FINANZAS))])
async def cierre_preview(
    request: Request,
    period_start: date = Query(..., alias="period_start"),
    period_end: date = Query(..., alias="period_end"),
    completed_only: bool = Query(False, alias="completed_only"),
    period_start_time: Optional[datetime] = Query(None, alias="period_start_time"),
    period_end_time: Optional[datetime] = Query(None, alias="period_end_time"),
    shift_template_id: Optional[UUID] = Query(None, alias="shift_template_id"),
):
    """
    Cierre X — non-destructive daily summary.

    Returns sales breakdown by payment method, gastos en efectivo,
    cash_expected, and open_tables_count.
    Safe to call multiple times — no writes.
    When period_start_time / period_end_time are provided, order filtering uses
    exact TIMESTAMPTZ comparison (supports cross-midnight shifts).
    """
    return await cierre_service.get_cierre_preview(
        request, period_start, period_end, completed_only,
        period_start_time=period_start_time,
        period_end_time=period_end_time,
        shift_template_id=shift_template_id,
    )


@router.post("", dependencies=[Depends(require_module(Module.FINANZAS))])
async def create_cierre(request: Request, body: CierreCreate):
    """
    Cierre Z — final daily close.

    Creates an accounting_period + closing_summary atomically.
    Returns 409 if:
    - The date range overlaps an existing closed period.
    - There are open table sessions and manager_override is false.
    """
    return await cierre_service.create_cierre(request, body)


@router.get("/day-window", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_day_window(
    request: Request,
    anchor_date: date = Query(..., alias="date", description="Tenant calendar day to resolve"),
):
    """Resolve full day vs remaining day after previous partial closes."""
    return await cierre_service.get_day_window(request, anchor_date)


@router.get("/day/window", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_day_window_safe(
    request: Request,
    anchor_date: date = Query(..., alias="date", description="Tenant calendar day to resolve"),
):
    """Resolve full day vs remaining day using a two-segment path that cannot match /{cierre_id}."""
    return await cierre_service.get_day_window(request, anchor_date)


@router.get("", dependencies=[Depends(require_module(Module.FINANZAS))])
async def list_cierres(
    request: Request,
    period_start: Optional[date] = Query(None, alias="period_start"),
    period_end:   Optional[date] = Query(None, alias="period_end"),
):
    """
    List arqueos for the tenant: open shifts first, then closed periods.
    Open shifts are always included; closed rows respect optional period_start/period_end filters.
    """
    return await cierre_service.list_cierres(request, period_start, period_end)


@router.get("/mensual", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_mensual(
    request: Request,
    year:  int = Query(..., alias="year"),
    month: int = Query(..., alias="month"),
):
    """
    Monthly close report — read-only aggregation of daily closes for the given month.
    Returns totals + list of individual daily closes + coverage metadata.
    """
    return await cierre_service.get_cierre_mensual(request, year, month)


@router.get("/mensual/{year}/{month}/status", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_monthly_period_status(
    year: int,
    month: int,
    request: Request,
    response: Response,
):
    """
    Get (or lazily create) the monthly accounting period record for the given year/month.
    Returns status 'open' or 'closed' plus metadata.
    Issue: #362
    """
    return await cierre_service.get_monthly_period(request, response, year, month)


@router.post("/mensual/{year}/{month}/close", dependencies=[Depends(require_module(Module.FINANZAS))])
async def close_monthly_period_endpoint(
    year: int,
    month: int,
    request: Request,
    response: Response,
    body: Optional[MonthlyPeriodClose] = None,
):
    """
    Close a monthly accounting period.
    Once closed, all orders whose order_date falls in that month become immutable.
    Returns 409 if the period is already closed.
    Issue: #362
    """
    notes = body.notes if body else None
    return await cierre_service.close_monthly_period(request, response, year, month, notes)


@router.get("/ultimo", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_ultimo_cierre(request: Request):
    """
    Returns the most recent closed period for the tenant, or null if none exists.
    Used by the new-cierre wizard to pre-fill the date range automatically.
    """
    return await cierre_service.get_ultimo_cierre(request)


@router.get("/suggested-window", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_suggested_cierre_window(
    request: Request,
    anchor_date: date = Query(..., alias="date", description="Fallback anchor date if no prior close exists"),
):
    """Suggest a custom cash-count window from last arqueo end through now (Bogotá)."""
    from app.services import shift_window_service

    return await shift_window_service.get_suggested_window(request, anchor_date)


@router.get("/shift-templates", dependencies=[Depends(require_module(Module.FINANZAS))])
async def list_cierre_shift_templates(request: Request):
    """Active shift templates for arqueo template mode (Finanzas-only)."""
    return await cierre_service.list_active_shift_templates(request)


@router.get("/shift-window", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_shift_window(
    request: Request,
    shift_template_id: UUID = Query(..., alias="shift_template_id"),
    anchor_date: date = Query(..., alias="date", description="Anchor calendar date (YYYY-MM-DD, Bogotá)"),
):
    """Finanzas-facing alias for template window resolution (same payload as operaciones)."""
    from app.services import shift_window_service

    return await shift_window_service.get_template_window(
        request, shift_template_id, anchor_date
    )


@router.post("/open-shift", dependencies=[Depends(require_module(Module.FINANZAS))])
async def open_cierre_shift(request: Request, body: OpenShiftCreate):
    """
    Open an operational shift with opening cash float (fondo de caja).
    Issue: warocol.com#920
    """
    return await cierre_service.open_shift(request, body)


@router.delete("/open-shift/{opening_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def delete_open_cierre_shift(request: Request, opening_id: UUID):
    """
    Cancel an open shift (fondo de caja) that has not been closed yet.
    Hard-deletes the cash_shift_openings row; no accounting_period is affected.
    """
    return await cierre_service.delete_open_shift(request, opening_id)


@router.get("/cash-settings", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_cash_settings(request: Request):
    """Tenant default opening cash float for arqueo (#922)."""
    return await cierre_service.get_cash_settings(request)


@router.patch("/cash-settings", dependencies=[Depends(require_module(Module.FINANZAS))])
async def patch_cierre_cash_settings(request: Request, body: CierreCashSettingsUpdate):
    """Update tenant default opening cash float (#922)."""
    return await cierre_service.update_cash_settings(request, body)


@router.get("/shift-status", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_shift_status(
    request: Request,
    period_start: date = Query(..., alias="period_start"),
    period_end: date = Query(..., alias="period_end"),
    period_start_time: Optional[datetime] = Query(None, alias="period_start_time"),
    period_end_time: Optional[datetime] = Query(None, alias="period_end_time"),
    shift_template_id: Optional[UUID] = Query(None, alias="shift_template_id"),
):
    """Return open shift for the resolved window, or status none. Issue: #920"""
    return await cierre_service.get_shift_status(
        request,
        period_start,
        period_end,
        period_start_time=period_start_time,
        period_end_time=period_end_time,
        shift_template_id=shift_template_id,
    )


@router.get("/reconciliations", dependencies=[Depends(require_module(Module.FINANZAS))])
async def list_cierre_reconciliations(
    request: Request,
    status: Optional[str] = Query(None),
    group_slug: Optional[str] = Query(None, alias="groupSlug"),
    date_from: Optional[date] = Query(None, alias="dateFrom"),
    date_to: Optional[date] = Query(None, alias="dateTo"),
    cierre_id: Optional[UUID] = Query(None, alias="cierreId"),
):
    """List non-cash payment method reconciliation rows for cierre."""
    return await cierre_service.list_reconciliations(
        request,
        status=status,
        group_slug=group_slug,
        date_from=date_from,
        date_to=date_to,
        cierre_id=cierre_id,
    )


@router.get("/reconciliations/{reconciliation_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre_reconciliation(request: Request, reconciliation_id: UUID):
    """Get a single payment method reconciliation row."""
    return await cierre_service.get_reconciliation(request, reconciliation_id)


@router.patch("/reconciliations/{reconciliation_id}/reported", dependencies=[Depends(require_module(Module.FINANZAS))])
async def update_cierre_reconciliation_reported(
    request: Request,
    reconciliation_id: UUID,
    body: CierreReconciliationReportedUpdate,
):
    """Update the reported/provider amount for a reconciliation row."""
    return await cierre_service.update_reconciliation_reported(request, reconciliation_id, body)


@router.post("/reconciliations/{reconciliation_id}/resolve", dependencies=[Depends(require_module(Module.FINANZAS))])
async def resolve_cierre_reconciliation(
    request: Request,
    reconciliation_id: UUID,
    body: CierreReconciliationResolve,
):
    """Resolve a reconciliation row and optionally create a draft PUC adjustment."""
    return await cierre_service.resolve_reconciliation(request, reconciliation_id, body)


@router.get("/{cierre_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_cierre(request: Request, cierre_id: UUID):
    """
    Full detail for a single closed period.
    Returns 404 if not found or belongs to a different tenant.
    """
    return await cierre_service.get_cierre(request, cierre_id)


@router.delete("/{cierre_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def delete_cierre(request: Request, cierre_id: UUID):
    """
    Soft-delete a closed period (sets deleted_at on accounting_period).
    The record is hidden from all list/detail queries but data is preserved.
    Returns 404 if not found or already deleted.
    """
    return await cierre_service.delete_cierre(request, cierre_id)
