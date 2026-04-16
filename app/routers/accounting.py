from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Request, Query
from app.services.accounting_service import (
    get_accounts,
    create_account,
    update_account,
    delete_account,
    create_journal_entry,
    list_journal_entries,
    get_journal_entry,
    post_journal_entry,
    void_journal_entry,
)
from app.models.accounting import (
    TenantAccountCreate,
    TenantAccountUpdate,
    TenantAccountResponse,
    TenantAccountsListResponse,
    JournalEntryCreate,
    JournalEntryResponse,
    JournalEntriesListResponse,
    JournalEntryVoidRequest,
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


# ---------------------------------------------------------------------------
# Journal Entries (#376)
# ---------------------------------------------------------------------------

@router.post("/journal-entries", response_model=JournalEntryResponse)
async def create_journal_entry_endpoint(request: Request, body: JournalEntryCreate):
    """
    Create a draft journal entry with lines.
    All account_ids must belong to the current tenant.
    source_module defaults to 'manual'. Status defaults to 'draft'.
    """
    return await create_journal_entry(request, body)


@router.get("/journal-entries", response_model=JournalEntriesListResponse)
async def list_journal_entries_endpoint(
    request: Request,
    status: Optional[str] = Query(None),
    source_module: Optional[str] = Query(None, alias="sourceModule"),
    date_from: Optional[str] = Query(None, alias="dateFrom"),
    date_to: Optional[str] = Query(None, alias="dateTo"),
    account_id: Optional[UUID] = Query(None, alias="accountId"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """
    List journal entries for the tenant with pagination.
    Filters: status, sourceModule, dateFrom, dateTo, accountId.
    """
    return await list_journal_entries(
        request,
        status=status,
        source_module=source_module,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
        page=page,
        limit=limit,
    )


@router.get("/journal-entries/{entry_id}", response_model=JournalEntryResponse)
async def get_journal_entry_endpoint(request: Request, entry_id: UUID):
    """
    Get a single journal entry with all its lines.
    """
    return await get_journal_entry(request, entry_id)


@router.post("/journal-entries/{entry_id}/post", response_model=JournalEntryResponse)
async def post_journal_entry_endpoint(request: Request, entry_id: UUID):
    """
    Post a draft entry to the GL.
    Validates: debits == credits, period not closed.
    Status changes: draft → posted. Once posted, entry is immutable.
    """
    return await post_journal_entry(request, entry_id)


@router.post("/journal-entries/{entry_id}/void", response_model=JournalEntryResponse)
async def void_journal_entry_endpoint(
    request: Request, entry_id: UUID, body: JournalEntryVoidRequest
):
    """
    Void a posted entry.
    Creates an auto-posted reversing entry (lines swapped) in the same transaction.
    Returns the reversing entry. Requires reason text.
    """
    return await void_journal_entry(request, entry_id, body.reason)
