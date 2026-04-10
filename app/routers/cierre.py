"""
Cierre Contable Router
Daily accounting close: preview (Cierre X) and final close wizard (Cierre Z).

Issue: https://github.com/uno0uno/warocol.com/issues/311
"""
from fastapi import APIRouter, Request, Query
from typing import Optional
from uuid import UUID
from datetime import date
from app.models.cierre import CierreCreate
from app.services import cierre_service

router = APIRouter(prefix="/cierre", tags=["cierre"])


@router.get("/preview")
async def cierre_preview(
    request: Request,
    period_start: date = Query(..., alias="period_start"),
    period_end: date = Query(..., alias="period_end"),
    completed_only: bool = Query(False, alias="completed_only"),
):
    """
    Cierre X — non-destructive daily summary.

    Returns sales breakdown by payment method, gastos en efectivo,
    cash_expected, and open_tables_count.
    Safe to call multiple times — no writes.
    """
    return await cierre_service.get_cierre_preview(request, period_start, period_end, completed_only)


@router.post("")
async def create_cierre(request: Request, body: CierreCreate):
    """
    Cierre Z — final daily close.

    Creates an accounting_period + closing_summary atomically.
    Returns 409 if:
    - The date range overlaps an existing closed period.
    - There are open table sessions and manager_override is false.
    """
    return await cierre_service.create_cierre(request, body)


@router.get("")
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


@router.get("/mensual")
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


@router.get("/{cierre_id}")
async def get_cierre(request: Request, cierre_id: UUID):
    """
    Full detail for a single closed period.
    Returns 404 if not found or belongs to a different tenant.
    """
    return await cierre_service.get_cierre(request, cierre_id)
