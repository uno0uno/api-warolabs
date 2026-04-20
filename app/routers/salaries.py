"""
Salary Management Router
Handles employee salary configuration and payment registration
"""
import logging
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException, Depends
from typing import List, Optional
from uuid import UUID
from decimal import Decimal
from datetime import datetime
from app.services.salary_service import (
    get_employees_with_salary,
    get_employee_salary_detail,
    configure_employee_salary,
    record_salary_payment,
    record_salary_payment_json,
    get_salary_payments,
    get_payment_detail,
    delete_salary_payment,
    update_salary_payment,
    get_salary_payment_history,
    upload_salary_payment_attachments,
    delete_salary_payment_attachment,
    record_prima_payment,
    get_prima_payments,
)
from app.models.salary import (
    EmployeesWithSalaryResponse,
    EmployeeDetailResponse,
    SalaryConfigResponse,
    SalaryPaymentResponse,
    SalaryPaymentsListResponse,
    EmployeeSalaryConfigCreate,
    SalaryPaymentCreate,
    PrimaPaymentCreate,
    PrimaPaymentResponse,
    PrimaPaymentListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# =============================================================================
# EMPLOYEES ENDPOINTS
# =============================================================================

@router.get("/employees", response_model=EmployeesWithSalaryResponse)
async def get_employees_endpoint(request: Request):
    """
    Get all employees with their salary configuration
    Returns employees with role != 'customer'
    """
    return await get_employees_with_salary(request)


@router.get("/employees/{employee_id}", response_model=EmployeeDetailResponse)
async def get_employee_detail_endpoint(request: Request, employee_id: UUID):
    """
    Get employee detail with salary config and payment history
    """
    return await get_employee_salary_detail(request, employee_id)


@router.post("/employees/{employee_id}/config", response_model=SalaryConfigResponse)
async def configure_salary_endpoint(
    request: Request,
    employee_id: UUID,
    config: EmployeeSalaryConfigCreate
):
    """
    Configure or update employee salary
    """
    return await configure_employee_salary(request, employee_id, config)


# =============================================================================
# PAYMENTS ENDPOINTS
# =============================================================================

@router.post("/payments", response_model=SalaryPaymentResponse)
async def record_payment_endpoint(
    request: Request,
    payment_data: SalaryPaymentCreate
):
    """
    Record a salary payment (JSON payload, no file attachments)
    Use POST /salaries/payments/{payment_id}/attachments to upload files after creation
    """
    return await record_salary_payment_json(request, payment_data)


@router.get("/payments", response_model=SalaryPaymentsListResponse)
async def get_payments_endpoint(
    request: Request,
    employee_id: Optional[UUID] = None,
    period_month: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """
    Get salary payments with optional filters
    """
    return await get_salary_payments(
        request=request,
        employee_id=employee_id,
        period_month=period_month,
        limit=limit,
        offset=offset
    )


@router.get("/payments/{payment_id}")
async def get_payment_detail_endpoint(request: Request, payment_id: UUID):
    """
    Get payment detail with attachments and employee info
    """
    return await get_payment_detail(request, payment_id)


@router.delete("/payments/{payment_id}")
async def delete_payment_endpoint(request: Request, payment_id: UUID):
    """
    Delete a salary payment
    """
    return await delete_salary_payment(request, payment_id)


@router.put("/payments/{payment_id}", response_model=SalaryPaymentResponse)
async def update_payment_endpoint(
    request: Request,
    payment_id: UUID,
    payment_amount: Optional[float] = Form(None),
    payment_date: Optional[str] = Form(None),
    payment_method: Optional[str] = Form(None),
    payment_reference: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    status: Optional[str] = Form(None)
):
    """
    Update an existing salary payment
    """
    # Parse payment_date if provided
    parsed_payment_date = None
    if payment_date:
        try:
            parsed_payment_date = datetime.fromisoformat(payment_date.replace('Z', '+00:00')) if 'T' in payment_date else datetime.strptime(payment_date, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payment_date format")

    return await update_salary_payment(
        request=request,
        payment_id=payment_id,
        payment_amount=Decimal(str(payment_amount)) if payment_amount is not None else None,
        payment_date=parsed_payment_date,
        payment_method=payment_method,
        payment_reference=payment_reference,
        notes=notes,
        status=status
    )


@router.get("/payments/{payment_id}/history")
async def get_payment_history_endpoint(
    request: Request,
    payment_id: UUID
):
    """
    Get change history for a salary payment
    """
    return await get_salary_payment_history(request, payment_id)


@router.post("/payments/{payment_id}/attachments")
async def upload_payment_attachments_endpoint(
    payment_id: UUID,
    request: Request,
    files: List[UploadFile] = File(...)
):
    """
    Upload attachments for a salary payment
    """
    return await upload_salary_payment_attachments(request, payment_id, files)


@router.delete("/payments/attachments/{attachment_id}")
async def delete_payment_attachment_endpoint(
    request: Request,
    attachment_id: UUID
):
    """
    Delete a salary payment attachment
    """
    return await delete_salary_payment_attachment(request, attachment_id)


# =============================================================================
# PRIMA DE SERVICIOS ENDPOINTS
# =============================================================================

@router.post("/employees/{member_id}/prima", response_model=PrimaPaymentResponse)
async def post_prima_payment_endpoint(
    request: Request,
    member_id: UUID,
    data: PrimaPaymentCreate,
):
    """
    Register prima de servicios payment for an employee.
    Only valid for employment_type='employee'.
    Returns 400 for daily/contractor workers.
    Returns 409 if prima for the same semestre was already paid.
    GL entry: DR 2620 / CR bank account (graceful degrade on failure).
    """
    return await record_prima_payment(request, member_id, data)


@router.get("/employees/{member_id}/prima", response_model=PrimaPaymentListResponse)
async def get_prima_payments_endpoint(
    request: Request,
    member_id: UUID,
):
    """
    List prima de servicios payments for an employee, ordered by payment_date DESC.
    """
    return await get_prima_payments(request, member_id)
