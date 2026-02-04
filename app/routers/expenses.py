from fastapi import APIRouter, Request, Response, HTTPException, Query, File, UploadFile, Form
from uuid import UUID
from typing import Optional, List
from app.services.expenses_service import (
    get_expense_categories,
    get_expenses_list,
    get_expense_by_id,
    create_expense,
    update_expense,
    delete_expense,
    get_expense_history,
    get_recurring_instances,
    get_recurring_instance_by_id,
    create_recurring_instance,
    update_recurring_instance,
    upload_instance_attachments,
    delete_instance_attachment
)
from app.models.expense import (
    ExpenseCreate,
    ExpenseUpdate,
    ExpenseResponse,
    ExpensesListResponse,
    ExpenseCategoriesResponse
)

router = APIRouter()

@router.get("/categories", response_model=ExpenseCategoriesResponse)
async def get_categories_endpoint(
    request: Request,
    response: Response
):
    """
    Get available expense categories
    """
    return await get_expense_categories(request, response)

@router.get("", response_model=ExpensesListResponse)
async def get_expenses_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    month_year: Optional[str] = Query(default=None, description="Filter by YYYY-MM"),
    category_id: Optional[UUID] = Query(default=None, description="Filter by Category ID"),
    search: Optional[str] = Query(default=None, description="Search term in description")
):
    """
    Get expenses list with tenant isolation and stats
    """
    return await get_expenses_list(
        request, response, page, limit, month_year, category_id, search
    )

@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense_by_id_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a single expense by ID with attachments
    """
    return await get_expense_by_id(request, response, expense_id)

@router.post("", response_model=ExpenseResponse)
async def create_expense_endpoint(
    request: Request,
    response: Response,
    transactionDate: str = Form(...),
    expenseCategoryId: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    isRecurring: str = Form(default="false"),
    frequency: Optional[str] = Form(default=None),
    recurringEndDate: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(default=[])
):
    """
    Create a new expense with optional file attachments and recurring settings
    """
    return await create_expense(
        request, response,
        transactionDate, expenseCategoryId, description, amount,
        isRecurring, frequency, recurringEndDate, files
    )

@router.put("/{expense_id}", response_model=ExpenseResponse)
async def update_expense_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response,
    transactionDate: str = Form(...),
    expenseCategoryId: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    isRecurring: str = Form(default="false"),
    frequency: Optional[str] = Form(default=None),
    recurringEndDate: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(default=[])
):
    """
    Update an existing expense with optional new file attachments and recurring settings
    """
    return await update_expense(
        request, response, expense_id,
        transactionDate, expenseCategoryId, description, amount,
        isRecurring, frequency, recurringEndDate, files
    )

@router.delete("/{expense_id}")
async def delete_expense_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Delete an expense
    """
    return await delete_expense(request, response, expense_id)

@router.get("/{expense_id}/history")
async def get_expense_history_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Get change history for an expense
    """
    return await get_expense_history(request, response, expense_id)

@router.get("/{expense_id}/instances")
async def get_expense_instances_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Get payment instances for a recurring expense
    """
    return await get_recurring_instances(request, response, expense_id)

@router.get("/instances/{instance_id}")
async def get_instance_by_id_endpoint(
    instance_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific payment instance by ID
    """
    return await get_recurring_instance_by_id(request, response, instance_id)

@router.post("/{expense_id}/instances")
async def create_expense_instance_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response,
    periodMonth: str = Form(...),
    scheduledDate: str = Form(...),
    amount: Optional[float] = Form(None),
    status: str = Form(default="pending"),
    paymentDate: Optional[str] = Form(None),
    paymentMethod: Optional[str] = Form(None),
    paymentReference: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[])
):
    """
    Create a new payment instance for a recurring expense with optional file attachments
    """
    return await create_recurring_instance(
        request, response, expense_id,
        periodMonth, scheduledDate, amount, status,
        paymentDate, paymentMethod, paymentReference, notes, files
    )

@router.put("/instances/{instance_id}")
async def update_expense_instance_endpoint(
    instance_id: UUID,
    request: Request,
    response: Response,
    status: Optional[str] = Form(None),
    paymentDate: Optional[str] = Form(None),
    paymentMethod: Optional[str] = Form(None),
    paymentReference: Optional[str] = Form(None),
    notes: Optional[str] = Form(None)
):
    """
    Update a payment instance (e.g., mark as paid)
    """
    return await update_recurring_instance(
        request, response, instance_id,
        status, paymentDate, paymentMethod, paymentReference, notes
    )

@router.post("/instances/{instance_id}/attachments")
async def upload_instance_attachments_endpoint(
    instance_id: UUID,
    request: Request,
    response: Response,
    files: List[UploadFile] = File(...)
):
    """
    Upload attachments for a payment instance
    """
    return await upload_instance_attachments(
        request, response, instance_id, files
    )

@router.delete("/instances/attachments/{attachment_id}")
async def delete_instance_attachment_endpoint(
    attachment_id: UUID,
    request: Request,
    response: Response
):
    """
    Delete an attachment from a payment instance
    """
    return await delete_instance_attachment(
        request, response, attachment_id
    )
