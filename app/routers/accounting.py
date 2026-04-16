from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Request, Query
from app.services.accounting_service import (
    get_accounts,
    create_account,
    update_account,
    delete_account,
)
from app.models.accounting import (
    TenantAccountCreate,
    TenantAccountUpdate,
    TenantAccountResponse,
    TenantAccountsListResponse,
)

router = APIRouter()


@router.get("/accounts", response_model=TenantAccountsListResponse)
async def list_accounts_endpoint(
    request: Request,
    account_class: Optional[str] = Query(None, alias="class"),
    account_type: Optional[str] = Query(None, alias="type"),
    active: Optional[bool] = Query(None),
):
    """
    Get the full chart of accounts for the authenticated tenant.
    Optional query filters: class (PUC class digit), type, active.
    Requires valid session cookie with selected tenant.
    """
    return await get_accounts(request, account_class=account_class, account_type=account_type, active=active)


@router.post("/accounts", response_model=TenantAccountResponse)
async def create_account_endpoint(request: Request, body: TenantAccountCreate):
    """
    Create a custom account for the current tenant.
    Code must be unique per tenant. is_system is always false for API-created accounts.
    Requires valid session cookie with selected tenant.
    """
    return await create_account(request, body)


@router.put("/accounts/{account_id}", response_model=TenantAccountResponse)
async def update_account_endpoint(request: Request, account_id: UUID, body: TenantAccountUpdate):
    """
    Update account name, active status, or is_detail flag.
    System accounts can be deactivated but not renamed.
    Requires valid session cookie with selected tenant.
    """
    return await update_account(request, account_id, body)


@router.delete("/accounts/{account_id}")
async def delete_account_endpoint(request: Request, account_id: UUID):
    """
    Soft-delete (deactivate) a custom account.
    System accounts and accounts with journal lines cannot be deleted.
    Requires valid session cookie with selected tenant.
    """
    return await delete_account(request, account_id)
