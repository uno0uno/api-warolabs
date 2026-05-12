from fastapi import Depends, APIRouter, Request, Response, Query, File, UploadFile
from app.core.permissions import Module, require_module
from uuid import UUID
from typing import Optional, List
from app.services.expenses_service import (
    get_expense_categories,
    get_expenses_list,
    get_expense_by_id,
    delete_expense,
    get_expense_history,
    get_recurring_instances,
    get_recurring_instance_by_id,
    upload_instance_attachments,
    delete_instance_attachment
)
from app.models.expense import (
    ExpenseCreate,
    ExpenseUpdate,
    ExpenseResponse,
    ExpensesListResponse,
    ExpenseCategoriesResponse,
    RecurringExpenseInstanceCreate,
    RecurringExpenseInstanceUpdate
)

router = APIRouter()

@router.get("/categories", response_model=ExpenseCategoriesResponse, dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_categories_endpoint(
    request: Request,
    response: Response
):
    """
    Get available expense categories
    """
    return await get_expense_categories(request, response)

@router.get("", response_model=ExpensesListResponse, dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_expenses_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    month_year: Optional[str] = Query(default=None, description="Filter by YYYY-MM"),
    category_id: Optional[UUID] = Query(default=None, description="Filter by Category ID"),
    search: Optional[str] = Query(default=None, description="Search term in description"),
    expense_type: Optional[str] = Query(default=None, description="Filter by expense type: cost, admin_expense, sales_expense")
):
    """
    Get expenses list with tenant isolation and stats
    """
    return await get_expenses_list(
        request, response, page, limit, month_year, category_id, search, expense_type
    )

@router.get("/{expense_id}", response_model=ExpenseResponse, dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_expense_by_id_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a single expense by ID with attachments
    """
    return await get_expense_by_id(request, response, expense_id)

@router.post("/{expense_id}/attachments", dependencies=[Depends(require_module(Module.FINANZAS))])
async def upload_expense_attachments_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response,
    files: List[UploadFile] = File(...)
):
    """
    Upload attachments for an expense after creation
    """
    from app.services.expenses_service import upload_expense_attachments
    return await upload_expense_attachments(request, response, expense_id, files)

@router.post("", response_model=ExpenseResponse, dependencies=[Depends(require_module(Module.FINANZAS))])
async def create_expense_endpoint(
    expense_data: ExpenseCreate,
    request: Request,
    response: Response
):
    """
    Create a new expense (JSON payload, no file attachments)
    Use POST /expenses/{expense_id}/attachments to upload files after creation
    """
    from app.services.expenses_service import create_expense_json
    return await create_expense_json(request, response, expense_data)

@router.put("/{expense_id}", response_model=ExpenseResponse, dependencies=[Depends(require_module(Module.FINANZAS))])
async def update_expense_endpoint(
    expense_id: UUID,
    expense_data: ExpenseUpdate,
    request: Request,
    response: Response
):
    """
    Update an existing expense (JSON payload)
    Use POST /expenses/{expense_id}/attachments to upload files
    """
    from app.services.expenses_service import update_expense_json
    return await update_expense_json(request, response, expense_id, expense_data)

@router.delete("/{expense_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def delete_expense_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Delete an expense
    """
    return await delete_expense(request, response, expense_id)

@router.get("/{expense_id}/history", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_expense_history_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Get change history for an expense
    """
    return await get_expense_history(request, response, expense_id)

@router.get("/{expense_id}/instances", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_expense_instances_endpoint(
    expense_id: UUID,
    request: Request,
    response: Response
):
    """
    Get payment instances for a recurring expense
    """
    return await get_recurring_instances(request, response, expense_id)

@router.get("/instances/{instance_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_instance_by_id_endpoint(
    instance_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific payment instance by ID
    """
    return await get_recurring_instance_by_id(request, response, instance_id)

@router.post("/{expense_id}/instances", dependencies=[Depends(require_module(Module.FINANZAS))])
async def create_expense_instance_endpoint(
    expense_id: UUID,
    instance_data: RecurringExpenseInstanceCreate,
    request: Request,
    response: Response
):
    """
    Create a new payment instance for a recurring expense (JSON payload)
    Use POST /instances/{instance_id}/attachments to upload files after creation
    """
    from app.services.expenses_service import create_recurring_instance_json
    return await create_recurring_instance_json(
        request, response, expense_id, instance_data
    )

@router.put("/instances/{instance_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
async def update_expense_instance_endpoint(
    instance_id: UUID,
    instance_data: RecurringExpenseInstanceUpdate,
    request: Request,
    response: Response
):
    """
    Update a payment instance (e.g., mark as paid) - JSON payload
    """
    from app.services.expenses_service import update_recurring_instance_json
    return await update_recurring_instance_json(
        request, response, instance_id, instance_data
    )

@router.post("/instances/{instance_id}/attachments", dependencies=[Depends(require_module(Module.FINANZAS))])
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

@router.delete("/instances/attachments/{attachment_id}", dependencies=[Depends(require_module(Module.FINANZAS))])
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
