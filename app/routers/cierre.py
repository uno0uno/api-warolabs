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
from app.models.cierre import CierreCreate, MonthlyPeriodClose
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


@router.get("", dependencies=[Depends(require_module(Module.FINANZAS))])
async def list_cierres(
    request: Request,
    period_start: Optional[date] = Query(None, alias="period_start"),
    period_end:   Optional[date] = Query(None, alias="period_end"),
):
    """
    List closed periods for the tenant, ordered by period_start DESC.
    Optionally filter by period_start >= period_start and period_end <= period_end.
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
