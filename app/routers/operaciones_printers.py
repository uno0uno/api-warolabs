"""Operaciones printer assignment API (warocol.com#1949 / #1951)."""
from fastapi import APIRouter, Depends, Request

from app.core.permissions import Module, require_any_module, require_module
from app.services.printer_assignments_service import (
    PrinterAssignmentsPut,
    get_printer_assignments,
    put_printer_assignments,
)

router = APIRouter(prefix="/operaciones", tags=["Operaciones Printers"])


@router.get(
    "/printers",
    dependencies=[
        Depends(
            require_any_module(
                Module.POS,
                Module.VENTAS,
                Module.DESPACHO,
                Module.OPERACIONES,
            )
        )
    ],
)
async def get_printers_endpoint(request: Request):
    """Return caja + per-station printer assignments and resolved fallbacks.

    Readable from POS/Ventas/Despacho so cashiers can route tickets without
    Operaciones module (warocol.com#1951). Writes stay Operaciones-only.
    """
    return await get_printer_assignments(request)


@router.put(
    "/printers",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def put_printers_endpoint(request: Request, body: PrinterAssignmentsPut):
    """Replace tenant printer assignments (caja + station map)."""
    return await put_printer_assignments(request, body)
