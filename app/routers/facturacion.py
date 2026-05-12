"""
Facturacion Router — bridge to api-facturacion (issue #129)

Acquirer lookup, catalog queries, and payroll documents.

All endpoints are stubs until api-facturacion implements the
corresponding upstream endpoints.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import Any, Dict, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module

_STUB_DETAIL = "Not yet available — api-facturacion endpoint pending"

# ── Acquirer lookup ──────────────────────────────────────────────────────────
acquirer_router = APIRouter(prefix="/api/acquirer", tags=["Facturacion"])


@acquirer_router.get("", dependencies=[Depends(require_module(Module.FACTURACION))])
async def lookup_acquirer(
    request: Request,
    type: Optional[str] = Query(None, description="Document type (e.g. 31 = NIT)"),
    number: Optional[str] = Query(None, description="Document number"),
) -> Dict[str, Any]:
    """
    Look up a DIAN-registered acquirer (buyer) by document type and number.

    Stub — returns 503 until api-facturacion /acquirer is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


# ── Catalog ──────────────────────────────────────────────────────────────────
catalog_router = APIRouter(prefix="/api/facturacion", tags=["Facturacion"])


@catalog_router.get("/catalog/{name}", dependencies=[Depends(require_module(Module.FACTURACION))])
async def get_catalog(
    request: Request,
    name: str,
) -> Dict[str, Any]:
    """
    Get a DIAN catalog by name (e.g. cities, taxes, units, document-types).

    Stub — returns 503 until api-facturacion /catalog/{name} is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


# ── Payroll ───────────────────────────────────────────────────────────────────
payroll_router = APIRouter(prefix="/api/payroll", tags=["Payroll"])


class PayrollEmitRequest(BaseModel):
    period: Optional[str] = Field(None, description="Payroll period (YYYY-MM)")
    employee_id: Optional[str] = Field(None, description="Employee UUID")


class PayrollReplaceRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for replacement")


class PayrollVoidRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for voiding payroll")


@payroll_router.post("/emit", dependencies=[Depends(require_module(Module.FACTURACION))])
async def emit_payroll(
    request: Request,
    body: PayrollEmitRequest,
) -> Dict[str, Any]:
    """
    Emit an electronic payroll document (nómina electrónica DIAN).

    Stub — returns 503 until api-facturacion /payroll/emit is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@payroll_router.post("/{payroll_id}/replace", dependencies=[Depends(require_module(Module.FACTURACION))])
async def replace_payroll(
    request: Request,
    payroll_id: UUID,
    body: PayrollReplaceRequest,
) -> Dict[str, Any]:
    """
    Replace (adjust) an existing electronic payroll document.

    Stub — returns 503 until api-facturacion /payroll/{id}/replace is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@payroll_router.post("/{payroll_id}/void", dependencies=[Depends(require_module(Module.FACTURACION))])
async def void_payroll(
    request: Request,
    payroll_id: UUID,
    body: PayrollVoidRequest,
) -> Dict[str, Any]:
    """
    Void (cancel) an electronic payroll document.

    Stub — returns 503 until api-facturacion /payroll/{id}/void is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)
