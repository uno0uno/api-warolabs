import logging
import asyncpg
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import date, datetime
from fastapi import Request, Response, HTTPException, UploadFile
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.expense import (
    Expense,
    ExpenseCreate,
    ExpenseUpdate,
    ExpenseCategory,
    ExpensesListResponse,
    ExpenseResponse,
    ExpenseCategoriesResponse,
    ExpensesStats,
    ExpenseChangeHistory,
    RecurringExpenseInstance,
    ChangeType,
    InstanceStatus
)
from app.services.purchase_tracking_service import upload_purchase_attachments
from app.services.aws_s3_service import AWSS3Service

logger = logging.getLogger(__name__)


def _looks_like_uuid(value: Optional[str]) -> bool:
    return bool(value and len(value) == 36 and '-' in value)


async def _resolve_payment_method(
    conn,
    tenant_id: UUID,
    payment_method: Optional[str],
    payment_method_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Resolve a group slug plus optional sub-method UUID for tenant expenses."""
    method_slug = payment_method or 'cash'
    method_id = payment_method_id

    if _looks_like_uuid(method_slug) and not method_id:
        method_id = method_slug
        method_slug = None

    if method_id:
        try:
            method_uuid = UUID(str(method_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Método de pago inválido")

        pm_row = await conn.fetchrow("""
            SELECT pm.id::text AS id, pmg.slug
            FROM payment_methods pm
            JOIN payment_method_groups pmg ON pmg.id = pm.group_id
            WHERE pm.id = $1
              AND pm.tenant_id = $2
              AND pm.is_active = true
        """, method_uuid, tenant_id)

        if not pm_row:
            raise HTTPException(status_code=400, detail="Método de pago inválido para este tenant")

        return pm_row["slug"], pm_row["id"]

    return method_slug or 'cash', None


def _raise_duplicate_expense_error(exc: asyncpg.UniqueViolationError) -> None:
    if getattr(exc, "constraint_name", "") == "tenant_expenses_tenant_id_expense_category_id_month_year_de_key":
        raise HTTPException(
            status_code=409,
            detail="Ya existe un gasto con la misma categoría, mes y descripción.",
        ) from exc
    raise exc


def _format_recurring_instance_response(instance_row) -> Dict[str, Any]:
    return {
        'success': True,
        'data': {
            'id': str(instance_row['id']),
            'tenantId': str(instance_row['tenant_id']),
            'expenseId': str(instance_row['expense_id']),
            'periodMonth': instance_row['period_month'],
            'scheduledDate': instance_row['scheduled_date'].isoformat(),
            'amount': float(instance_row['amount']),
            'status': instance_row['status'],
            'paymentDate': instance_row['payment_date'].isoformat() if instance_row['payment_date'] else None,
            'paymentMethod': instance_row['payment_method'],
            'paymentReference': instance_row['payment_reference'],
            'notes': instance_row['notes'],
            'createdBy': str(instance_row['created_by']) if instance_row['created_by'] else None,
            'createdAt': instance_row['created_at'].isoformat(),
            'updatedAt': instance_row['updated_at'].isoformat(),
            'attachments': []
        }
    }


async def get_next_expense_number(conn, tenant_id: UUID) -> str:
    """Generate the next WR-GTO-YYYY-NNNN number for a tenant."""
    current_year = datetime.now().year
    prefix = f'WR-GTO-{current_year}-'
    last = await conn.fetchrow("""
        SELECT expense_number FROM tenant_expenses
        WHERE tenant_id = $1 AND expense_number LIKE $2
        ORDER BY expense_number DESC LIMIT 1
    """, tenant_id, f'{prefix}%')
    if last and last['expense_number']:
        try:
            next_number = int(last['expense_number'].split('-')[-1]) + 1
        except (ValueError, IndexError):
            next_number = 1
    else:
        next_number = 1
    return f"{prefix}{next_number:04d}"


def _parse_date(value):
    """Parse ISO 8601 date string safely on Python < 3.11 (handles Z suffix)."""
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


# ---------------------------------------------------------------------------
# GL helpers — Issue #377 (auto-posting gastos → GL)
# ---------------------------------------------------------------------------

async def _post_expense_gl_entry(
    conn,
    tenant_id: UUID,
    expense_id: UUID,
    amount: float,
    transaction_date,           # datetime.date object
    description: str,
    category_code: str,
    payment_method: Optional[str],
) -> None:
    """
    Post an auto GL entry for a gastos expense.
    Silently skips if: no mapping found, account missing, or period closed.
    Caller must wrap in try/except for graceful degrade.
    """
    # 1. Look up GL mapping for this tenant × category
    mapping = await conn.fetchrow(
        """SELECT debit_account_code, credit_cash_account_code, credit_default_account_code
           FROM expense_category_gl_mappings
           WHERE tenant_id = $1 AND category_code = $2""",
        tenant_id, category_code,
    )
    if not mapping:
        logger.warning(
            f"[GL] No mapping for category '{category_code}' on tenant {tenant_id} — skip GL post"
        )
        return

    debit_code = mapping['debit_account_code']
    credit_code = (
        mapping['credit_cash_account_code']
        if payment_method == 'cash'
        else mapping['credit_default_account_code']
    )

    # 2. Resolve account UUIDs from codes
    debit_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, debit_code,
    )
    credit_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not debit_acct or not credit_acct:
        logger.warning(
            f"[GL] Account not found (debit={debit_code}, credit={credit_code}) "
            f"for tenant {tenant_id} — skip GL post"
        )
        return

    # 3. Check period is open
    period_year = transaction_date.year
    period_month = transaction_date.month
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, period_year, period_month,
    )
    if closed:
        logger.warning(
            f"[GL] Period {period_year}-{period_month:02d} is closed — "
            f"skip GL post for expense {expense_id}"
        )
        return

    # 4. Insert header + lines atomically
    amount_val = float(Decimal(str(amount)))
    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'gastos', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, transaction_date, period_year, period_month,
            description, expense_id, amount_val, amount_val,
        )
        entry_id = entry_row['id']

        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, debit_acct['id'], amount_val, description,
        )
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, credit_acct['id'], amount_val, description,
        )

    logger.info(
        f"[GL] ✅ Posted entry {entry_id} for expense {expense_id} "
        f"(debit={debit_code}, credit={credit_code})"
    )


async def _void_expense_gl_entry(
    conn,
    tenant_id: UUID,
    expense_id: UUID,
    reason: str = "Gasto modificado o eliminado",
) -> None:
    """
    Find and void the most recent posted GL entry for a gastos expense.
    Silently skips if no entry found (pre-#377 expense) or period is closed.
    Caller must wrap in try/except for graceful degrade.
    """
    entry = await conn.fetchrow(
        """SELECT id, entry_date, period_year, period_month, description,
                  total_debit, total_credit
           FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'gastos' AND source_id = $2
                 AND status = 'posted'
           ORDER BY created_at DESC
           LIMIT 1""",
        tenant_id, expense_id,
    )
    if not entry:
        logger.info(f"[GL] No posted GL entry for expense {expense_id} — skip void")
        return

    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry['period_year'], entry['period_month'],
    )
    if closed:
        logger.warning(
            f"[GL] Period {entry['period_year']}-{entry['period_month']:02d} is closed — "
            f"skip GL void for expense {expense_id}"
        )
        return

    original_lines = await conn.fetch(
        """SELECT account_id, debit, credit, description, line_order
           FROM tenant_journal_lines
           WHERE journal_entry_id = $1 ORDER BY line_order""",
        entry['id'],
    )

    async with conn.transaction():
        await conn.execute(
            "UPDATE tenant_journal_entries SET status = 'voided', voided_at = NOW() WHERE id = $1",
            entry['id'],
        )

        rev_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'system', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry['entry_date'], entry['period_year'], entry['period_month'],
            f"Reversión: {entry['description']} — {reason}",
            entry['id'],
            float(entry['total_debit']), float(entry['total_credit']),
        )
        rev_id = rev_row['id']

        for line in original_lines:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                rev_id, line['account_id'],
                float(line['credit']), float(line['debit']),
                line['description'], line['line_order'],
            )

    logger.info(
        f"[GL] ✅ Voided GL entry {entry['id']} → reversing {rev_id} for expense {expense_id}"
    )


async def get_expense_categories(
    request: Request,
    response: Response
) -> ExpenseCategoriesResponse:
    """
    Get all available expense categories
    """
    try:
        require_valid_session(request)
        
        async with get_db_connection(use_transaction=False) as conn:
            categories_data = await conn.fetch("""
                SELECT
                    id,
                    category_code,
                    category_name,
                    description,
                    is_active
                FROM expense_categories
                WHERE is_active = true
                ORDER BY category_name ASC
            """)
            
            categories = []
            for row in categories_data:
                categories.append(ExpenseCategory(
                    id=row['id'],
                    categoryCode=row['category_code'],
                    categoryName=row['category_name'],
                    description=row['description'],
                    isActive=row['is_active']
                ))
                
            return ExpenseCategoriesResponse(data=categories)
            
    except Exception as e:
        logger.error(f"Error fetching expense categories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_expenses_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    month_year: Optional[str] = None,
    category_id: Optional[UUID] = None,
    search: Optional[str] = None,
    expense_type: Optional[str] = None
) -> ExpensesListResponse:
    """
    Get expenses list with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Base query with recurring fields
            base_query = """
                SELECT
                    e.id,
                    e.tenant_id,
                    e.expense_category_id,
                    e.month_year,
                    e.amount,
                    e.description,
                    e.source_system,
                    e.expense_number,
                    e.created_at,
                    e.transaction_date,
                    e.is_recurring,
                    e.frequency,
                    e.recurring_end_date,
                    e.payment_method,
                    e.payment_method_id::text as payment_method_id,
                    e.expense_type,
                    c.id as cat_id,
                    c.category_code,
                    c.category_name,
                    c.description as cat_description,
                    c.is_active as cat_active
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.tenant_id = $1
            """
            
            params = [tenant_id]
            param_count = 2
            
            # Filters
            if month_year:
                base_query += f" AND e.month_year = ${param_count}"
                params.append(month_year)
                param_count += 1
                
            if category_id:
                base_query += f" AND e.expense_category_id = ${param_count}"
                params.append(category_id)
                param_count += 1
                
            if search:
                base_query += f" AND (LOWER(e.description) LIKE LOWER(${param_count}) OR LOWER(e.expense_number) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1

            if expense_type:
                base_query += f" AND e.expense_type = ${param_count}"
                params.append(expense_type)
                param_count += 1
            
            # Count query for pagination
            count_query = f"SELECT COUNT(*) as total FROM ({base_query}) as subquery"
            count_result = await conn.fetchrow(count_query, *params)
            
            # Stats calculation (based on current filters, but maybe user wants stats for the whole month regardless of search? 
            # Usually stats reflect what's on screen or the whole period. Let's do whole period if month_year is set, otherwise all time match)
            
            # Let's calculate stats based on the same filters to be consistent
            stats_query = f"""
                SELECT 
                    SUM(amount) as total_amount,
                    expense_category_id
                FROM ({base_query}) as subquery
                GROUP BY expense_category_id
            """
            stats_data = await conn.fetch(stats_query, *params)
            
            total_amount = 0.0
            by_category = {}
            
            # We need to map category IDs back to names for the stats
            # But the stats_query returns IDs. We can look them up from the main results or fetch them.
            # actually better to just group by category_name in the stats query
            
            stats_query_named = f"""
                SELECT 
                    SUM(e.amount) as total_amount,
                    c.category_name
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.tenant_id = $1
            """
            # Re-apply filters to stats query
            stats_params = [tenant_id]
            stats_p_count = 2
             
            if month_year:
                stats_query_named += f" AND e.month_year = ${stats_p_count}"
                stats_params.append(month_year)
                stats_p_count += 1
            if category_id:
                stats_query_named += f" AND e.expense_category_id = ${stats_p_count}"
                stats_params.append(category_id)
                stats_p_count += 1
            if search:
                stats_query_named += f" AND (LOWER(e.description) LIKE LOWER(${stats_p_count}))"
                stats_params.append(f"%{search}%")
                stats_p_count += 1
                
            stats_query_named += " GROUP BY c.category_name"
            
            stats_rows = await conn.fetch(stats_query_named, *stats_params)
            
            for row in stats_rows:
                amount = float(row['total_amount'])
                total_amount += amount
                by_category[row['category_name']] = amount

            # Pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY e.transaction_date DESC, e.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])
            
            expenses_data = await conn.fetch(base_query, *params)
            
            expenses = []
            for row in expenses_data:
                category = ExpenseCategory(
                    id=row['cat_id'],
                    categoryCode=row['category_code'],
                    categoryName=row['category_name'],
                    description=row['cat_description'],
                    isActive=row['cat_active']
                )
                
                expenses.append(Expense(
                    id=row['id'],
                    tenantId=row['tenant_id'],
                    expenseCategoryId=row['expense_category_id'],
                    monthYear=row['month_year'],
                    amount=float(row['amount']),
                    description=row['description'],
                    sourceSystem=row['source_system'],
                    expenseNumber=row['expense_number'],
                    createdAt=row['created_at'],
                    transactionDate=row['transaction_date'],
                    isRecurring=row['is_recurring'],
                    frequency=row['frequency'],
                    recurringEndDate=row['recurring_end_date'],
                    paymentMethod=row['payment_method'],
                    paymentMethodId=row['payment_method_id'],
                    expenseType=row['expense_type'],
                    category=category
                ))
                
            return ExpensesListResponse(
                data=expenses,
                total=count_result['total'],
                page=page,
                limit=limit,
                stats=ExpensesStats(
                    totalAmount=total_amount,
                    count=count_result['total'],
                    byCategory=by_category
                )
            )

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error fetching expenses: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_expense_by_id(
    request: Request,
    response: Response,
    expense_id: UUID
) -> ExpenseResponse:
    """
    Get a single expense by ID with attachments
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Fetch expense with category
            full_expense = await conn.fetchrow("""
                SELECT
                    e.id,
                    e.tenant_id,
                    e.expense_category_id,
                    e.month_year,
                    e.amount,
                    e.description,
                    e.source_system,
                    e.expense_number,
                    e.created_at,
                    e.transaction_date,
                    e.is_recurring,
                    e.frequency,
                    e.recurring_end_date,
                    e.payment_method,
                    e.payment_method_id::text as payment_method_id,
                    e.expense_type,
                    c.id as cat_id,
                    c.category_code,
                    c.category_name,
                    c.description as cat_description,
                    c.is_active as cat_active
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.id = $1 AND e.tenant_id = $2
            """, expense_id, tenant_id)
            
            if not full_expense:
                raise HTTPException(status_code=404, detail="Expense not found")
            
            # Get attachments
            attachments_data = await conn.fetch("""
                SELECT
                    id,
                    expense_id,
                    tenant_id,
                    file_name,
                    file_size,
                    mime_type,
                    attachment_type,
                    description,
                    uploaded_by,
                    uploaded_at,
                    s3_key
                FROM purchase_attachments
                WHERE expense_id = $1
                ORDER BY uploaded_at DESC
            """, expense_id)
            
            # Generate presigned URLs for attachments
            s3_service = AWSS3Service()
            attachments = []
            for row in attachments_data:
                att_dict = dict(row)
                if att_dict.get('s3_key'):
                    try:
                        presigned_url = await s3_service.get_presigned_url(
                            att_dict['s3_key'],
                            expiration=3600
                        )
                        att_dict['s3_url'] = presigned_url
                    except Exception as e:
                        logger.error(f"Error generating presigned URL: {e}")
                        att_dict['s3_url'] = None
                else:
                    att_dict['s3_url'] = None
                attachments.append(att_dict)
            
            category = ExpenseCategory(
                id=full_expense['cat_id'],
                categoryCode=full_expense['category_code'],
                categoryName=full_expense['category_name'],
                description=full_expense['cat_description'],
                isActive=full_expense['cat_active']
            )
            
            expense = Expense(
                id=full_expense['id'],
                tenantId=full_expense['tenant_id'],
                expenseCategoryId=full_expense['expense_category_id'],
                monthYear=full_expense['month_year'],
                amount=float(full_expense['amount']),
                description=full_expense['description'],
                sourceSystem=full_expense['source_system'],
                expenseNumber=full_expense['expense_number'],
                createdAt=full_expense['created_at'],
                transactionDate=full_expense['transaction_date'],
                isRecurring=full_expense['is_recurring'],
                frequency=full_expense['frequency'],
                recurringEndDate=full_expense['recurring_end_date'],
                paymentMethod=full_expense['payment_method'],
                paymentMethodId=full_expense['payment_method_id'],
                expenseType=full_expense['expense_type'],
                category=category,
                attachments=attachments
            )

            return ExpenseResponse(data=expense)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching expense: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_expense(
    request: Request,
    response: Response,
    transaction_date: str,
    expense_category_id: str,
    description: str,
    amount: float,
    is_recurring: str = "false",
    frequency: Optional[str] = None,
    recurring_end_date: Optional[str] = None,
    files: List[UploadFile] = []
) -> ExpenseResponse:
    """
    Create a new expense with optional file attachments and recurring settings
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse transaction_date
        trans_date = _parse_date(transaction_date).date()
        month_year = trans_date.strftime("%Y-%m")

        # Parse expense_category_id
        category_uuid = UUID(expense_category_id)

        # Parse is_recurring (Form sends "true"/"false" strings)
        is_recurring_bool = is_recurring.lower() == "true"

        # Parse recurring_end_date if provided
        recurring_end_date_parsed = None
        if recurring_end_date:
            recurring_end_date_parsed = _parse_date(recurring_end_date).date()

        # Validate: if is_recurring is True, frequency must be provided
        if is_recurring_bool and not frequency:
            raise HTTPException(
                status_code=400,
                detail="Frequency is required for recurring expenses"
            )

        # Validate: frequency must be valid enum value
        if frequency:
            valid_frequencies = ['weekly', 'biweekly', 'monthly', 'quarterly', 'yearly']
            if frequency.lower() not in valid_frequencies:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"
                )

        # Validate: recurring_end_date must be >= transaction_date
        if recurring_end_date_parsed and recurring_end_date_parsed < trans_date:
            raise HTTPException(
                status_code=400,
                detail="Recurring end date must be on or after the transaction date"
            )

        async with get_db_connection() as conn:
            # Verify category exists
            category_exists = await conn.fetchval("""
                SELECT EXISTS(SELECT 1 FROM expense_categories WHERE id = $1)
            """, category_uuid)
            
            if not category_exists:
                raise HTTPException(status_code=400, detail="Invalid expense category")

            # Generate expense number inside the same connection
            expense_number = await get_next_expense_number(conn, tenant_id)

            # Insert expense with recurring fields
            row = await conn.fetchrow("""
                INSERT INTO tenant_expenses (
                    tenant_id,
                    expense_category_id,
                    month_year,
                    amount,
                    description,
                    transaction_date,
                    source_system,
                    expense_number,
                    is_recurring,
                    frequency,
                    recurring_end_date
                ) VALUES ($1, $2, $3, $4, $5, $6, 'manual', $7, $8, $9, $10)
                RETURNING id, created_at
            """,
                tenant_id,
                category_uuid,
                month_year,
                amount,
                description,
                trans_date,
                expense_number,
                is_recurring_bool,
                frequency.lower() if frequency else None,
                recurring_end_date_parsed
            )
            
            expense_id = row['id']
            
            # Upload attachments if provided
            if files:
                s3_service = AWSS3Service()
                for file in files:
                    try:
                        # Upload file to S3
                        s3_key = await s3_service.upload_file(
                            file_content=file.file,
                            filename=file.filename,
                            folder='expenses/attachments',
                            content_type=file.content_type
                        )
                        
                        if s3_key:
                            # Generate presigned URL
                            file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)
                            
                            # Save attachment record with expense_id
                            await conn.execute("""
                                INSERT INTO purchase_attachments (
                                    tenant_id,
                                    expense_id,
                                    path,
                                    file_name,
                                    file_size,
                                    mime_type,
                                    attachment_type,
                                    description,
                                    uploaded_by,
                                    s3_key,
                                    s3_url
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            """,
                                tenant_id,
                                expense_id,
                                s3_key,
                                file.filename,
                                file.size or 0,
                                file.content_type or 'application/octet-stream',
                                'invoice',
                                f'Soporte: {description[:50]}',
                                user_id,
                                s3_key,
                                file_url
                            )
                    except Exception as e:
                        logger.error(f"Error uploading attachment {file.filename}: {str(e)}")
            
            # Fetch full expense with category and new recurring fields
            full_expense = await conn.fetchrow("""
                SELECT
                    e.id,
                    e.tenant_id,
                    e.expense_category_id,
                    e.month_year,
                    e.amount,
                    e.description,
                    e.source_system,
                    e.expense_number,
                    e.created_at,
                    e.transaction_date,
                    e.is_recurring,
                    e.frequency,
                    e.recurring_end_date,
                    e.payment_method,
                    c.id as cat_id,
                    c.category_code,
                    c.category_name,
                    c.description as cat_description,
                    c.is_active as cat_active
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.id = $1
            """, expense_id)

            category = ExpenseCategory(
                id=full_expense['cat_id'],
                categoryCode=full_expense['category_code'],
                categoryName=full_expense['category_name'],
                description=full_expense['cat_description'],
                isActive=full_expense['cat_active']
            )

            expense = Expense(
                id=full_expense['id'],
                tenantId=full_expense['tenant_id'],
                expenseCategoryId=full_expense['expense_category_id'],
                monthYear=full_expense['month_year'],
                amount=float(full_expense['amount']),
                description=full_expense['description'],
                sourceSystem=full_expense['source_system'],
                expenseNumber=full_expense['expense_number'],
                createdAt=full_expense['created_at'],
                transactionDate=full_expense['transaction_date'],
                isRecurring=full_expense['is_recurring'],
                frequency=full_expense['frequency'],
                recurringEndDate=full_expense['recurring_end_date'],
                paymentMethod=full_expense['payment_method'],
                category=category
            )

            # Post GL entry — graceful degrade: never fail the expense save
            try:
                await _post_expense_gl_entry(
                    conn, tenant_id, expense_id,
                    float(full_expense['amount']),
                    full_expense['transaction_date'],
                    full_expense['description'],
                    full_expense['category_code'],
                    full_expense['payment_method'],
                )
            except Exception as _gl_err:
                logger.warning(f"[GL] GL post failed for expense {expense_id}: {_gl_err}")

            return ExpenseResponse(data=expense)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating expense: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_expense_json(
    request: Request,
    response: Response,
    expense_data: ExpenseCreate
) -> ExpenseResponse:
    """
    Create a new expense from JSON payload (no file attachments)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Validate: if is_recurring is True, frequency must be provided
        if expense_data.is_recurring and not expense_data.frequency:
            raise HTTPException(
                status_code=400,
                detail="Frequency is required for recurring expenses"
            )

        # Validate: recurring_end_date must be >= transaction_date
        if expense_data.recurring_end_date and expense_data.recurring_end_date < expense_data.transaction_date:
            raise HTTPException(
                status_code=400,
                detail="Recurring end date must be on or after the transaction date"
            )

        month_year = expense_data.transaction_date.strftime("%Y-%m")

        async with get_db_connection() as conn:
            # Verify category exists
            category_exists = await conn.fetchval("""
                SELECT EXISTS(SELECT 1 FROM expense_categories WHERE id = $1)
            """, expense_data.expense_category_id)

            if not category_exists:
                raise HTTPException(status_code=400, detail="Invalid expense category")

            payment_method_raw, payment_method_id = await _resolve_payment_method(
                conn,
                tenant_id,
                expense_data.payment_method,
                expense_data.payment_method_id,
            )

            # Generate expense number inside the same connection
            expense_number = await get_next_expense_number(conn, tenant_id)

            # Insert expense
            row = await conn.fetchrow("""
                INSERT INTO tenant_expenses (
                    tenant_id,
                    expense_category_id,
                    month_year,
                    amount,
                    description,
                    transaction_date,
                    source_system,
                    expense_number,
                    is_recurring,
                    frequency,
                    recurring_end_date,
                    payment_method,
                    payment_method_id,
                    expense_type
                ) VALUES ($1, $2, $3, $4, $5, $6, 'manual', $7, $8, $9, $10, $11, $12::uuid, $13)
                RETURNING id, created_at
            """,
                tenant_id,
                expense_data.expense_category_id,
                month_year,
                expense_data.amount,
                expense_data.description,
                expense_data.transaction_date,
                expense_number,
                expense_data.is_recurring,
                expense_data.frequency,
                expense_data.recurring_end_date,
                payment_method_raw,
                payment_method_id,
                expense_data.expense_type
            )

            expense_id = row['id']

            # Fetch full expense with category
            full_expense = await conn.fetchrow("""
                SELECT
                    e.id,
                    e.tenant_id,
                    e.expense_category_id,
                    e.month_year,
                    e.amount,
                    e.description,
                    e.source_system,
                    e.expense_number,
                    e.created_at,
                    e.transaction_date,
                    e.is_recurring,
                    e.frequency,
                    e.recurring_end_date,
                    e.payment_method,
                    e.payment_method_id::text as payment_method_id,
                    e.expense_type,
                    c.id as cat_id,
                    c.category_code,
                    c.category_name,
                    c.description as cat_description,
                    c.is_active as cat_active
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.id = $1
            """, expense_id)

            category = ExpenseCategory(
                id=full_expense['cat_id'],
                categoryCode=full_expense['category_code'],
                categoryName=full_expense['category_name'],
                description=full_expense['cat_description'],
                isActive=full_expense['cat_active']
            )

            expense = Expense(
                id=full_expense['id'],
                tenantId=full_expense['tenant_id'],
                expenseCategoryId=full_expense['expense_category_id'],
                monthYear=full_expense['month_year'],
                amount=full_expense['amount'],
                description=full_expense['description'],
                sourceSystem=full_expense['source_system'],
                expenseNumber=full_expense['expense_number'],
                createdAt=full_expense['created_at'],
                transactionDate=full_expense['transaction_date'],
                isRecurring=full_expense['is_recurring'],
                frequency=full_expense['frequency'],
                recurringEndDate=full_expense['recurring_end_date'],
                paymentMethod=full_expense['payment_method'],
                paymentMethodId=full_expense['payment_method_id'],
                expenseType=full_expense['expense_type'],
                category=category
            )

            # Post GL entry — graceful degrade: never fail the expense save
            try:
                await _post_expense_gl_entry(
                    conn, tenant_id, expense_id,
                    float(full_expense['amount']),
                    full_expense['transaction_date'],
                    full_expense['description'],
                    full_expense['category_code'],
                    full_expense['payment_method'],
                )
            except Exception as _gl_err:
                logger.warning(f"[GL] GL post failed for expense {expense_id}: {_gl_err}")

            return ExpenseResponse(data=expense)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError as e:
        _raise_duplicate_expense_error(e)
    except Exception as e:
        logger.error(f"Error creating expense: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def _track_change(
    conn,
    tenant_id: UUID,
    expense_id: UUID,
    change_type: str,
    user_id: Optional[UUID] = None,
    field_changed: Optional[str] = None,
    old_value: Optional[Any] = None,
    new_value: Optional[Any] = None,
    expense_snapshot: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None
):
    """
    Helper function to insert a record into expense_change_history
    """
    await conn.execute("""
        INSERT INTO expense_change_history (
            tenant_id,
            expense_id,
            change_type,
            field_changed,
            old_value,
            new_value,
            expense_snapshot,
            changed_by,
            notes
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    """,
        tenant_id,
        expense_id,
        change_type,
        field_changed,
        old_value,
        new_value,
        expense_snapshot,
        user_id,
        notes
    )

async def update_expense(
    request: Request,
    response: Response,
    expense_id: UUID,
    transaction_date: str,
    expense_category_id: str,
    description: str,
    amount: float,
    is_recurring: str = "false",
    frequency: Optional[str] = None,
    recurring_end_date: Optional[str] = None,
    files: List[UploadFile] = []
) -> ExpenseResponse:
    """
    Update an expense with optional new file attachments and recurring settings
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse inputs
        trans_date = _parse_date(transaction_date).date()
        month_year = trans_date.strftime("%Y-%m")
        category_uuid = UUID(expense_category_id)

        # Parse is_recurring (Form sends "true"/"false" strings)
        is_recurring_bool = is_recurring.lower() == "true"

        # Parse recurring_end_date if provided
        recurring_end_date_parsed = None
        if recurring_end_date:
            recurring_end_date_parsed = _parse_date(recurring_end_date).date()

        # Validate: if is_recurring is True, frequency must be provided
        if is_recurring_bool and not frequency:
            raise HTTPException(
                status_code=400,
                detail="Frequency is required for recurring expenses"
            )

        # Validate: frequency must be valid enum value
        if frequency:
            valid_frequencies = ['weekly', 'biweekly', 'monthly', 'quarterly', 'yearly']
            if frequency.lower() not in valid_frequencies:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"
                )

        # Validate: recurring_end_date must be >= transaction_date
        if recurring_end_date_parsed and recurring_end_date_parsed < trans_date:
            raise HTTPException(
                status_code=400,
                detail="Recurring end date must be on or after the transaction date"
            )

        async with get_db_connection() as conn:
            # Fetch old values for change tracking
            old_expense = await conn.fetchrow("""
                SELECT
                    expense_category_id,
                    amount,
                    description,
                    transaction_date,
                    is_recurring,
                    frequency,
                    recurring_end_date
                FROM tenant_expenses
                WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not old_expense:
                raise HTTPException(status_code=404, detail="Expense not found")
            
            # Update expense with recurring fields
            await conn.execute("""
                UPDATE tenant_expenses
                SET expense_category_id = $1,
                    amount = $2,
                    description = $3,
                    transaction_date = $4,
                    month_year = $5,
                    is_recurring = $6,
                    frequency = $7,
                    recurring_end_date = $8
                WHERE id = $9 AND tenant_id = $10
            """,
                category_uuid,
                amount,
                description,
                trans_date,
                month_year,
                is_recurring_bool,
                frequency.lower() if frequency else None,
                recurring_end_date_parsed,
                expense_id,
                tenant_id
            )

            # Track changes
            changes_tracked = []

            # Compare and track each field
            if old_expense['expense_category_id'] != category_uuid:
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='expense_category_id',
                    old_value={'category_id': str(old_expense['expense_category_id'])},
                    new_value={'category_id': str(category_uuid)}
                )
                changes_tracked.append('category')

            if float(old_expense['amount']) != amount:
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='amount',
                    old_value={'amount': float(old_expense['amount'])},
                    new_value={'amount': amount}
                )
                changes_tracked.append('amount')

            if old_expense['description'] != description:
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='description',
                    old_value={'description': old_expense['description']},
                    new_value={'description': description}
                )
                changes_tracked.append('description')

            if old_expense['transaction_date'] != trans_date:
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='transaction_date',
                    old_value={'transaction_date': old_expense['transaction_date'].isoformat()},
                    new_value={'transaction_date': trans_date.isoformat()}
                )
                changes_tracked.append('transaction_date')

            if old_expense['is_recurring'] != is_recurring_bool:
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='is_recurring',
                    old_value={'is_recurring': old_expense['is_recurring']},
                    new_value={'is_recurring': is_recurring_bool}
                )
                changes_tracked.append('is_recurring')

            if old_expense['frequency'] != (frequency.lower() if frequency else None):
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='frequency',
                    old_value={'frequency': old_expense['frequency']},
                    new_value={'frequency': frequency.lower() if frequency else None}
                )
                changes_tracked.append('frequency')

            if old_expense['recurring_end_date'] != recurring_end_date_parsed:
                await _track_change(
                    conn, tenant_id, expense_id, 'field_update', user_id,
                    field_changed='recurring_end_date',
                    old_value={'recurring_end_date': old_expense['recurring_end_date'].isoformat() if old_expense['recurring_end_date'] else None},
                    new_value={'recurring_end_date': recurring_end_date_parsed.isoformat() if recurring_end_date_parsed else None}
                )
                changes_tracked.append('recurring_end_date')

            if changes_tracked:
                logger.info(f"Tracked {len(changes_tracked)} changes for expense {expense_id}: {changes_tracked}")

            # Upload new attachments if provided
            if files:
                s3_service = AWSS3Service()
                for file in files:
                    try:
                        s3_key = await s3_service.upload_file(
                            file_content=file.file,
                            filename=file.filename,
                            folder='expenses/attachments',
                            content_type=file.content_type
                        )
                        
                        if s3_key:
                            file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)
                            
                            await conn.execute("""
                                INSERT INTO purchase_attachments (
                                    tenant_id,
                                    expense_id,
                                    path,
                                    file_name,
                                    file_size,
                                    mime_type,
                                    attachment_type,
                                    description,
                                    uploaded_by,
                                    s3_key,
                                    s3_url
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            """,
                                tenant_id,
                                expense_id,
                                s3_key,
                                file.filename,
                                file.size or 0,
                                file.content_type or 'application/octet-stream',
                                'invoice',
                                f'Soporte adicional: {description[:50]}',
                                user_id,
                                s3_key,
                                file_url
                            )
                    except Exception as e:
                        logger.error(f"Error uploading attachment {file.filename}: {str(e)}")
            
            # Fetch updated
            full_expense = await conn.fetchrow("""
                SELECT
                    e.id,
                    e.tenant_id,
                    e.expense_category_id,
                    e.month_year,
                    e.amount,
                    e.description,
                    e.source_system,
                    e.created_at,
                    e.transaction_date,
                    e.is_recurring,
                    e.frequency,
                    e.recurring_end_date,
                    e.payment_method,
                    c.id as cat_id,
                    c.category_code,
                    c.category_name,
                    c.description as cat_description,
                    c.is_active as cat_active
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.id = $1
            """, expense_id)
            
            category = ExpenseCategory(
                id=full_expense['cat_id'],
                categoryCode=full_expense['category_code'],
                categoryName=full_expense['category_name'],
                description=full_expense['cat_description'],
                isActive=full_expense['cat_active']
            )
            
            expense = Expense(
                id=full_expense['id'],
                tenantId=full_expense['tenant_id'],
                expenseCategoryId=full_expense['expense_category_id'],
                monthYear=full_expense['month_year'],
                amount=float(full_expense['amount']),
                description=full_expense['description'],
                sourceSystem=full_expense['source_system'],
                createdAt=full_expense['created_at'],
                transactionDate=full_expense['transaction_date'],
                isRecurring=full_expense['is_recurring'],
                frequency=full_expense['frequency'],
                recurringEndDate=full_expense['recurring_end_date'],
                paymentMethod=full_expense['payment_method'],
                category=category
            )

            # Update GL: void old entry + post new (graceful degrade)
            try:
                async with conn.transaction():
                    await _void_expense_gl_entry(conn, tenant_id, expense_id, "Gasto actualizado")
                    await _post_expense_gl_entry(
                        conn, tenant_id, expense_id,
                        float(full_expense['amount']),
                        full_expense['transaction_date'],
                        full_expense['description'],
                        full_expense['category_code'],
                        full_expense['payment_method'],
                    )
            except Exception as _gl_err:
                logger.warning(f"[GL] GL update failed for expense {expense_id}: {_gl_err}")

            return ExpenseResponse(data=expense)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating expense: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def update_expense_json(
    request: Request,
    response: Response,
    expense_id: UUID,
    expense_data: ExpenseUpdate
) -> ExpenseResponse:
    """
    Update an expense from JSON payload (no file attachments)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify expense exists and belongs to tenant
            old_expense = await conn.fetchrow("""
                SELECT * FROM tenant_expenses
                WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not old_expense:
                raise HTTPException(status_code=404, detail="Expense not found")

            # Build update fields
            update_fields = []
            update_values = []
            param_count = 1

            if expense_data.transaction_date is not None:
                update_fields.append(f"transaction_date = ${param_count}")
                update_values.append(expense_data.transaction_date)
                param_count += 1
                update_fields.append(f"month_year = ${param_count}")
                update_values.append(expense_data.transaction_date.strftime("%Y-%m"))
                param_count += 1

            if expense_data.expense_category_id is not None:
                update_fields.append(f"expense_category_id = ${param_count}")
                update_values.append(expense_data.expense_category_id)
                param_count += 1

            if expense_data.amount is not None:
                update_fields.append(f"amount = ${param_count}")
                update_values.append(expense_data.amount)
                param_count += 1

            if expense_data.description is not None:
                update_fields.append(f"description = ${param_count}")
                update_values.append(expense_data.description)
                param_count += 1

            if expense_data.is_recurring is not None:
                update_fields.append(f"is_recurring = ${param_count}")
                update_values.append(expense_data.is_recurring)
                param_count += 1

            if expense_data.frequency is not None:
                update_fields.append(f"frequency = ${param_count}")
                update_values.append(expense_data.frequency)
                param_count += 1

            if expense_data.recurring_end_date is not None:
                update_fields.append(f"recurring_end_date = ${param_count}")
                update_values.append(expense_data.recurring_end_date)
                param_count += 1

            if expense_data.payment_method is not None:
                upd_payment_method, upd_payment_method_id = await _resolve_payment_method(
                    conn,
                    tenant_id,
                    expense_data.payment_method,
                    expense_data.payment_method_id,
                )
                update_fields.append(f"payment_method = ${param_count}")
                update_values.append(upd_payment_method)
                param_count += 1
                update_fields.append(f"payment_method_id = ${param_count}::uuid")
                update_values.append(upd_payment_method_id)
                param_count += 1

            if expense_data.expense_type is not None:
                update_fields.append(f"expense_type = ${param_count}")
                update_values.append(expense_data.expense_type)
                param_count += 1

            if update_fields:
                update_values.extend([expense_id, tenant_id])
                await conn.execute(f"""
                    UPDATE tenant_expenses
                    SET {', '.join(update_fields)}
                    WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                """, *update_values)

            # Fetch updated expense
            full_expense = await conn.fetchrow("""
                SELECT
                    e.id, e.tenant_id, e.expense_category_id, e.month_year,
                    e.amount, e.description, e.source_system, e.created_at,
                    e.transaction_date, e.is_recurring, e.frequency, e.recurring_end_date,
                    e.payment_method, e.payment_method_id::text as payment_method_id, e.expense_type,
                    c.id as cat_id, c.category_code, c.category_name,
                    c.description as cat_description, c.is_active as cat_active
                FROM tenant_expenses e
                JOIN expense_categories c ON e.expense_category_id = c.id
                WHERE e.id = $1
            """, expense_id)

            category = ExpenseCategory(
                id=full_expense['cat_id'],
                categoryCode=full_expense['category_code'],
                categoryName=full_expense['category_name'],
                description=full_expense['cat_description'],
                isActive=full_expense['cat_active']
            )

            expense = Expense(
                id=full_expense['id'],
                tenantId=full_expense['tenant_id'],
                expenseCategoryId=full_expense['expense_category_id'],
                monthYear=full_expense['month_year'],
                amount=full_expense['amount'],
                description=full_expense['description'],
                sourceSystem=full_expense['source_system'],
                createdAt=full_expense['created_at'],
                transactionDate=full_expense['transaction_date'],
                isRecurring=full_expense['is_recurring'],
                frequency=full_expense['frequency'],
                recurringEndDate=full_expense['recurring_end_date'],
                paymentMethod=full_expense['payment_method'],
                paymentMethodId=full_expense['payment_method_id'],
                category=category
            )

            # Update GL: void old entry + post new (graceful degrade)
            try:
                async with conn.transaction():
                    await _void_expense_gl_entry(conn, tenant_id, expense_id, "Gasto actualizado")
                    await _post_expense_gl_entry(
                        conn, tenant_id, expense_id,
                        float(full_expense['amount']),
                        full_expense['transaction_date'],
                        full_expense['description'],
                        full_expense['category_code'],
                        full_expense['payment_method'],
                    )
            except Exception as _gl_err:
                logger.warning(f"[GL] GL update failed for expense {expense_id}: {_gl_err}")

            return ExpenseResponse(data=expense)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError as e:
        _raise_duplicate_expense_error(e)
    except Exception as e:
        logger.error(f"Error updating expense: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def delete_expense(
    request: Request,
    response: Response,
    expense_id: UUID
) -> Dict[str, Any]:
    """
    Delete an expense
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Fetch expense to check period before deleting
            expense_row = await conn.fetchrow(
                "SELECT transaction_date FROM tenant_expenses WHERE id = $1 AND tenant_id = $2",
                expense_id, tenant_id,
            )
            if not expense_row:
                raise HTTPException(status_code=404, detail="Expense not found")

            # Block deletion if period is closed
            period_year = expense_row['transaction_date'].year
            period_month = expense_row['transaction_date'].month
            closed = await conn.fetchval(
                """SELECT 1 FROM tenant_monthly_periods
                   WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
                tenant_id, period_year, period_month,
            )
            if closed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"No se puede eliminar un gasto de un período cerrado "
                        f"({period_year}-{period_month:02d})"
                    ),
                )

            # Void GL entry if one exists (graceful degrade)
            try:
                async with conn.transaction():
                    await _void_expense_gl_entry(conn, tenant_id, expense_id, "Gasto eliminado")
            except Exception as _gl_err:
                logger.warning(f"[GL] GL void failed on delete for expense {expense_id}: {_gl_err}")

            result = await conn.execute(
                "DELETE FROM tenant_expenses WHERE id = $1 AND tenant_id = $2",
                expense_id, tenant_id,
            )
            if result == "DELETE 0":
                raise HTTPException(status_code=404, detail="Expense not found")

            return {"success": True, "message": "Expense deleted successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting expense: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def upload_expense_attachments(
    request: Request,
    response: Response,
    expense_id: UUID,
    files: List[UploadFile]
) -> Dict[str, Any]:
    """
    Upload attachments to an existing expense
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify expense exists and belongs to tenant
            expense = await conn.fetchrow("""
                SELECT id, description FROM tenant_expenses
                WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not expense:
                raise HTTPException(status_code=404, detail="Expense not found")

            # Upload files
            uploaded_files = []
            if files:
                s3_service = AWSS3Service()
                for file in files:
                    try:
                        # Upload file to S3
                        s3_key = await s3_service.upload_file(
                            file_content=file.file,
                            filename=file.filename,
                            folder='expenses/attachments',
                            content_type=file.content_type
                        )

                        if s3_key:
                            file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)

                            # Insert attachment record
                            await conn.execute("""
                                INSERT INTO purchase_attachments (
                                    tenant_id,
                                    expense_id,
                                    s3_key,
                                    file_name,
                                    file_size,
                                    mime_type,
                                    attachment_type,
                                    description,
                                    uploaded_by,
                                    path,
                                    s3_url
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            """,
                                tenant_id,
                                expense_id,
                                s3_key,
                                file.filename,
                                file.size or 0,
                                file.content_type or 'application/octet-stream',
                                'invoice',
                                f'Soporte: {expense["description"][:50]}',
                                user_id,
                                s3_key,
                                file_url
                            )
                            uploaded_files.append({
                                "filename": file.filename,
                                "url": file_url
                            })
                    except Exception as e:
                        logger.error(f"Error uploading attachment {file.filename}: {str(e)}")

            return {
                "success": True,
                "message": f"Se subieron {len(uploaded_files)} archivos correctamente",
                "files": uploaded_files
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading expense attachments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_expense_history(
    request: Request,
    response: Response,
    expense_id: UUID
) -> List[Dict[str, Any]]:
    """
    Get change history for a specific expense
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Verify expense belongs to tenant
            expense = await conn.fetchrow("""
                SELECT id FROM tenant_expenses WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not expense:
                raise HTTPException(status_code=404, detail="Expense not found")

            # Fetch history with user info
            history_rows = await conn.fetch("""
                SELECT
                    h.id,
                    h.tenant_id,
                    h.expense_id,
                    h.change_type,
                    h.field_changed,
                    h.old_value,
                    h.new_value,
                    h.expense_snapshot,
                    h.changed_by,
                    h.changed_at,
                    h.notes,
                    h.created_at,
                    p.email as changed_by_email,
                    p.name as changed_by_name
                FROM expense_change_history h
                LEFT JOIN profile p ON h.changed_by = p.id
                WHERE h.expense_id = $1 AND h.tenant_id = $2
                ORDER BY h.changed_at DESC
            """, expense_id, tenant_id)

            history = []
            for row in history_rows:
                history_item = {
                    'id': str(row['id']),
                    'tenantId': str(row['tenant_id']),
                    'expenseId': str(row['expense_id']),
                    'changeType': row['change_type'],
                    'fieldChanged': row['field_changed'],
                    'oldValue': row['old_value'],
                    'newValue': row['new_value'],
                    'expenseSnapshot': row['expense_snapshot'],
                    'changedBy': str(row['changed_by']) if row['changed_by'] else None,
                    'changedAt': row['changed_at'].isoformat(),
                    'notes': row['notes'],
                    'createdAt': row['created_at'].isoformat(),
                    'changedByEmail': row['changed_by_email'],
                    'changedByName': row['changed_by_name']
                }
                history.append(history_item)

            return history

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching expense history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_recurring_instance_by_id(
    request: Request,
    response: Response,
    instance_id: UUID
) -> Dict[str, Any]:
    """
    Get a specific payment instance by ID with expense details and attachments
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Fetch instance with expense details
            instance_row = await conn.fetchrow("""
                SELECT
                    i.id,
                    i.tenant_id,
                    i.expense_id,
                    i.period_month,
                    i.scheduled_date,
                    i.amount,
                    i.status,
                    i.payment_date,
                    i.payment_method,
                    i.payment_reference,
                    i.notes,
                    i.created_by,
                    i.created_at,
                    i.updated_at,
                    e.description as expense_description,
                    e.expense_category_id,
                    e.transaction_date as expense_date,
                    ec.category_name
                FROM recurring_expense_instances i
                JOIN tenant_expenses e ON i.expense_id = e.id
                LEFT JOIN expense_categories ec ON e.expense_category_id = ec.id
                WHERE i.id = $1 AND i.tenant_id = $2
            """, instance_id, tenant_id)

            if not instance_row:
                raise HTTPException(status_code=404, detail="Instance not found")

            # Get attachments for this instance
            attachments_data = await conn.fetch("""
                SELECT
                    id,
                    file_name,
                    file_size,
                    mime_type,
                    uploaded_by,
                    uploaded_at,
                    s3_key
                FROM purchase_attachments
                WHERE recurring_instance_id = $1
                ORDER BY uploaded_at DESC
            """, instance_id)

            # Generate presigned URLs
            s3_service = AWSS3Service()
            attachments = []
            for att_row in attachments_data:
                att_dict = dict(att_row)
                if att_dict.get('s3_key'):
                    try:
                        presigned_url = await s3_service.get_presigned_url(
                            att_dict['s3_key'],
                            expiration=3600
                        )
                        att_dict['s3_url'] = presigned_url
                    except Exception as e:
                        logger.error(f"Error generating presigned URL: {e}")
                        att_dict['s3_url'] = None
                attachments.append(att_dict)

            instance = {
                'id': instance_row['id'],
                'tenantId': instance_row['tenant_id'],
                'expenseId': instance_row['expense_id'],
                'periodMonth': instance_row['period_month'],
                'scheduledDate': instance_row['scheduled_date'].isoformat() if instance_row['scheduled_date'] else None,
                'amount': float(instance_row['amount']),
                'status': instance_row['status'],
                'paymentDate': instance_row['payment_date'].isoformat() if instance_row['payment_date'] else None,
                'paymentMethod': instance_row['payment_method'],
                'paymentReference': instance_row['payment_reference'],
                'notes': instance_row['notes'],
                'createdBy': instance_row['created_by'],
                'createdAt': instance_row['created_at'].isoformat() if instance_row['created_at'] else None,
                'updatedAt': instance_row['updated_at'].isoformat() if instance_row['updated_at'] else None,
                'attachments': attachments,
                'expense': {
                    'id': instance_row['expense_id'],
                    'description': instance_row['expense_description'],
                    'expenseCategoryId': instance_row['expense_category_id'],
                    'categoryName': instance_row['category_name'],
                    'transactionDate': instance_row['expense_date'].isoformat() if instance_row['expense_date'] else None
                }
            }

            return instance

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching instance by ID: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_recurring_instances(
    request: Request,
    response: Response,
    expense_id: UUID
) -> List[Dict[str, Any]]:
    """
    Get all payment instances for a recurring expense
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Verify expense is recurring and belongs to tenant
            expense = await conn.fetchrow("""
                SELECT id, is_recurring FROM tenant_expenses
                WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not expense:
                raise HTTPException(status_code=404, detail="Expense not found")

            if not expense['is_recurring']:
                raise HTTPException(status_code=400, detail="Expense is not recurring")

            # Fetch instances
            instances_rows = await conn.fetch("""
                SELECT
                    i.id,
                    i.tenant_id,
                    i.expense_id,
                    i.period_month,
                    i.scheduled_date,
                    i.amount,
                    i.status,
                    i.payment_date,
                    i.payment_method,
                    i.payment_reference,
                    i.notes,
                    i.created_by,
                    i.created_at,
                    i.updated_at
                FROM recurring_expense_instances i
                WHERE i.expense_id = $1 AND i.tenant_id = $2
                ORDER BY i.scheduled_date DESC
            """, expense_id, tenant_id)

            instances = []
            for row in instances_rows:
                # Get attachments for this instance
                attachments_data = await conn.fetch("""
                    SELECT
                        id,
                        file_name,
                        file_size,
                        mime_type,
                        uploaded_by,
                        uploaded_at,
                        s3_key
                    FROM purchase_attachments
                    WHERE recurring_instance_id = $1
                    ORDER BY uploaded_at DESC
                """, row['id'])

                # Generate presigned URLs
                s3_service = AWSS3Service()
                attachments = []
                for att_row in attachments_data:
                    att_dict = {
                        'id': str(att_row['id']),
                        'fileName': att_row['file_name'],
                        'fileSize': att_row['file_size'],
                        'mimeType': att_row['mime_type'],
                        'uploadedBy': str(att_row['uploaded_by']) if att_row['uploaded_by'] else None,
                        'uploadedAt': att_row['uploaded_at'].isoformat() if att_row['uploaded_at'] else None
                    }
                    if att_row['s3_key']:
                        try:
                            presigned_url = await s3_service.get_presigned_url(
                                att_row['s3_key'],
                                expiration=3600
                            )
                            att_dict['s3Url'] = presigned_url
                        except Exception as e:
                            logger.error(f"Error generating presigned URL: {e}")
                            att_dict['s3Url'] = None
                    attachments.append(att_dict)

                instance = {
                    'id': str(row['id']),
                    'tenantId': str(row['tenant_id']),
                    'expenseId': str(row['expense_id']),
                    'periodMonth': row['period_month'],
                    'scheduledDate': row['scheduled_date'].isoformat(),
                    'amount': float(row['amount']),
                    'status': row['status'],
                    'paymentDate': row['payment_date'].isoformat() if row['payment_date'] else None,
                    'paymentMethod': row['payment_method'],
                    'paymentReference': row['payment_reference'],
                    'notes': row['notes'],
                    'createdBy': str(row['created_by']) if row['created_by'] else None,
                    'createdAt': row['created_at'].isoformat(),
                    'updatedAt': row['updated_at'].isoformat(),
                    'attachments': attachments
                }
                instances.append(instance)

            return instances

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching recurring instances: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_recurring_instance(
    request: Request,
    response: Response,
    expense_id: UUID,
    period_month: str,
    scheduled_date: str,
    amount: Optional[float] = None,
    status: str = "pending",
    payment_date: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_reference: Optional[str] = None,
    notes: Optional[str] = None,
    files: List[UploadFile] = []
) -> Dict[str, Any]:
    """
    Create a new payment instance for a recurring expense
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse dates
        scheduled_date_parsed = _parse_date(scheduled_date).date()
        payment_date_parsed = None
        if payment_date:
            payment_date_parsed = _parse_date(payment_date)

        async with get_db_connection() as conn:
            # Verify expense is recurring
            expense = await conn.fetchrow("""
                SELECT id, is_recurring, amount FROM tenant_expenses
                WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not expense:
                raise HTTPException(status_code=404, detail="Expense not found")

            if not expense['is_recurring']:
                raise HTTPException(status_code=400, detail="Expense is not recurring")

            # Use expense amount if not provided
            instance_amount = amount if amount is not None else float(expense['amount'])

            # Insert instance
            instance_id = await conn.fetchval("""
                INSERT INTO recurring_expense_instances (
                    tenant_id,
                    expense_id,
                    period_month,
                    scheduled_date,
                    amount,
                    status,
                    payment_date,
                    payment_method,
                    payment_reference,
                    notes,
                    created_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
            """,
                tenant_id,
                expense_id,
                period_month,
                scheduled_date_parsed,
                instance_amount,
                status,
                payment_date_parsed,
                payment_method,
                payment_reference,
                notes,
                user_id
            )

            # Upload attachments if provided
            uploaded_attachments = []
            if files and len(files) > 0:
                s3_service = AWSS3Service()
                for file in files:
                    try:
                        # Upload to S3
                        s3_key = await s3_service.upload_file(
                            file_content=file.file,
                            filename=file.filename,
                            folder='expenses/instance-attachments',
                            content_type=file.content_type
                        )

                        if s3_key:
                            # Generate presigned URL
                            file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)

                            # Save to database
                            attachment_id = await conn.fetchval("""
                                INSERT INTO purchase_attachments (
                                    tenant_id,
                                    recurring_instance_id,
                                    path,
                                    file_name,
                                    file_size,
                                    mime_type,
                                    attachment_type,
                                    description,
                                    uploaded_by,
                                    s3_key,
                                    s3_url
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                                RETURNING id
                            """,
                                tenant_id,
                                instance_id,
                                s3_key,
                                file.filename,
                                file.size or 0,
                                file.content_type or 'application/octet-stream',
                                'payment_proof',
                                f'Comprobante de pago - {file.filename}',
                                user_id,
                                s3_key,
                                file_url
                            )

                            uploaded_attachments.append({
                                'id': str(attachment_id),
                                'fileName': file.filename,
                                'fileSize': file.size or 0,
                                'mimeType': file.content_type or 'application/octet-stream',
                                's3Url': file_url
                            })

                    except Exception as e:
                        logger.error(f"Error uploading file {file.filename}: {e}")
                        # Continue with other files

            # Fetch created instance
            instance_row = await conn.fetchrow("""
                SELECT * FROM recurring_expense_instances WHERE id = $1
            """, instance_id)

            return {
                'id': str(instance_row['id']),
                'tenantId': str(instance_row['tenant_id']),
                'expenseId': str(instance_row['expense_id']),
                'periodMonth': instance_row['period_month'],
                'scheduledDate': instance_row['scheduled_date'].isoformat(),
                'amount': float(instance_row['amount']),
                'status': instance_row['status'],
                'paymentDate': instance_row['payment_date'].isoformat() if instance_row['payment_date'] else None,
                'paymentMethod': instance_row['payment_method'],
                'paymentReference': instance_row['payment_reference'],
                'notes': instance_row['notes'],
                'createdBy': str(instance_row['created_by']) if instance_row['created_by'] else None,
                'createdAt': instance_row['created_at'].isoformat(),
                'updatedAt': instance_row['updated_at'].isoformat(),
                'attachments': uploaded_attachments
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating recurring instance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def update_recurring_instance(
    request: Request,
    response: Response,
    instance_id: UUID,
    status: Optional[str] = None,
    payment_date: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_reference: Optional[str] = None,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update a payment instance (e.g., mark as paid)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse payment_date if provided
        payment_date_parsed = None
        if payment_date:
            payment_date_parsed = _parse_date(payment_date)

        async with get_db_connection() as conn:
            # Verify instance exists
            instance = await conn.fetchrow("""
                SELECT id FROM recurring_expense_instances
                WHERE id = $1 AND tenant_id = $2
            """, instance_id, tenant_id)

            if not instance:
                raise HTTPException(status_code=404, detail="Instance not found")

            # Build dynamic UPDATE query
            update_fields = []
            params = []
            param_count = 1

            if status is not None:
                update_fields.append(f"status = ${param_count}")
                params.append(status)
                param_count += 1

            if payment_date_parsed is not None:
                update_fields.append(f"payment_date = ${param_count}")
                params.append(payment_date_parsed)
                param_count += 1

            if payment_method is not None:
                update_fields.append(f"payment_method = ${param_count}")
                params.append(payment_method)
                param_count += 1

            if payment_reference is not None:
                update_fields.append(f"payment_reference = ${param_count}")
                params.append(payment_reference)
                param_count += 1

            if notes is not None:
                update_fields.append(f"notes = ${param_count}")
                params.append(notes)
                param_count += 1

            update_fields.append(f"updated_at = NOW()")

            if len(params) == 0:
                raise HTTPException(status_code=400, detail="No fields to update")

            params.extend([instance_id, tenant_id])
            query = f"""
                UPDATE recurring_expense_instances
                SET {', '.join(update_fields)}
                WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
            """

            await conn.execute(query, *params)

            # Fetch updated instance
            updated_instance = await conn.fetchrow("""
                SELECT * FROM recurring_expense_instances WHERE id = $1
            """, instance_id)

            # Get attachments
            attachments_data = await conn.fetch("""
                SELECT id, file_name, file_size, mime_type, uploaded_by, uploaded_at, s3_key
                FROM purchase_attachments
                WHERE recurring_instance_id = $1
            """, instance_id)

            s3_service = AWSS3Service()
            attachments = []
            for row in attachments_data:
                att_dict = {
                    'id': str(row['id']),
                    'fileName': row['file_name'],
                    'fileSize': row['file_size'],
                    'mimeType': row['mime_type'],
                    'uploadedBy': str(row['uploaded_by']) if row['uploaded_by'] else None,
                    'uploadedAt': row['uploaded_at'].isoformat() if row['uploaded_at'] else None
                }
                if row['s3_key']:
                    try:
                        presigned_url = await s3_service.get_presigned_url(row['s3_key'], expiration=3600)
                        att_dict['s3Url'] = presigned_url
                    except Exception as e:
                        logger.error(f"Error generating presigned URL: {e}")
                        att_dict['s3Url'] = None
                attachments.append(att_dict)

            return {
                'id': str(updated_instance['id']),
                'tenantId': str(updated_instance['tenant_id']),
                'expenseId': str(updated_instance['expense_id']),
                'periodMonth': updated_instance['period_month'],
                'scheduledDate': updated_instance['scheduled_date'].isoformat(),
                'amount': float(updated_instance['amount']),
                'status': updated_instance['status'],
                'paymentDate': updated_instance['payment_date'].isoformat() if updated_instance['payment_date'] else None,
                'paymentMethod': updated_instance['payment_method'],
                'paymentReference': updated_instance['payment_reference'],
                'notes': updated_instance['notes'],
                'createdBy': str(updated_instance['created_by']) if updated_instance['created_by'] else None,
                'createdAt': updated_instance['created_at'].isoformat(),
                'updatedAt': updated_instance['updated_at'].isoformat(),
                'attachments': attachments
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating recurring instance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def create_recurring_instance_json(
    request: Request,
    response: Response,
    expense_id: UUID,
    instance_data: 'RecurringExpenseInstanceCreate'
) -> Dict[str, Any]:
    """
    Create a new payment instance for a recurring expense (JSON payload)
    """
    from app.models.expense import RecurringExpenseInstanceCreate

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse dates
        scheduled_date_parsed = _parse_date(instance_data.scheduled_date).date() if isinstance(instance_data.scheduled_date, str) else instance_data.scheduled_date
        payment_date_parsed = None
        if instance_data.payment_date:
            payment_date_parsed = _parse_date(instance_data.payment_date)

        async with get_db_connection() as conn:
            # Verify expense is recurring
            expense = await conn.fetchrow("""
                SELECT id, is_recurring, amount FROM tenant_expenses
                WHERE id = $1 AND tenant_id = $2
            """, expense_id, tenant_id)

            if not expense:
                raise HTTPException(status_code=404, detail="Expense not found")

            if not expense['is_recurring']:
                raise HTTPException(status_code=400, detail="Expense is not recurring")

            # Use expense amount if not provided
            instance_amount = instance_data.amount if instance_data.amount is not None else float(expense['amount'])

            # Insert instance. Retrying the same recurring period returns the
            # existing row instead of surfacing a duplicate-key failure.
            instance_id = await conn.fetchval("""
                INSERT INTO recurring_expense_instances (
                    tenant_id,
                    expense_id,
                    period_month,
                    scheduled_date,
                    amount,
                    status,
                    payment_date,
                    payment_method,
                    payment_reference,
                    notes,
                    created_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (expense_id, period_month)
                DO UPDATE SET period_month = EXCLUDED.period_month
                RETURNING id
            """,
                tenant_id,
                expense_id,
                instance_data.period_month,
                scheduled_date_parsed,
                instance_amount,
                instance_data.status,
                payment_date_parsed,
                instance_data.payment_method,
                instance_data.payment_reference,
                instance_data.notes,
                user_id
            )

            # Fetch created instance
            instance_row = await conn.fetchrow("""
                SELECT * FROM recurring_expense_instances WHERE id = $1
            """, instance_id)

            return _format_recurring_instance_response(instance_row)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating recurring instance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def update_recurring_instance_json(
    request: Request,
    response: Response,
    instance_id: UUID,
    instance_data: 'RecurringExpenseInstanceUpdate'
) -> Dict[str, Any]:
    """
    Update a payment instance (JSON payload)
    """
    from app.models.expense import RecurringExpenseInstanceUpdate

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse payment_date if provided
        payment_date_parsed = None
        if instance_data.payment_date:
            payment_date_parsed = _parse_date(instance_data.payment_date)

        async with get_db_connection() as conn:
            # Verify instance exists
            instance = await conn.fetchrow("""
                SELECT id FROM recurring_expense_instances
                WHERE id = $1 AND tenant_id = $2
            """, instance_id, tenant_id)

            if not instance:
                raise HTTPException(status_code=404, detail="Instance not found")

            # Build dynamic UPDATE query
            update_fields = []
            params = []
            param_count = 1

            if instance_data.status is not None:
                update_fields.append(f"status = ${param_count}")
                params.append(instance_data.status)
                param_count += 1

            if payment_date_parsed is not None:
                update_fields.append(f"payment_date = ${param_count}")
                params.append(payment_date_parsed)
                param_count += 1

            if instance_data.payment_method is not None:
                update_fields.append(f"payment_method = ${param_count}")
                params.append(instance_data.payment_method)
                param_count += 1

            if instance_data.payment_reference is not None:
                update_fields.append(f"payment_reference = ${param_count}")
                params.append(instance_data.payment_reference)
                param_count += 1

            if instance_data.notes is not None:
                update_fields.append(f"notes = ${param_count}")
                params.append(instance_data.notes)
                param_count += 1

            update_fields.append(f"updated_at = NOW()")

            if len(params) == 0:
                raise HTTPException(status_code=400, detail="No fields to update")

            params.extend([instance_id, tenant_id])
            query = f"""
                UPDATE recurring_expense_instances
                SET {', '.join(update_fields)}
                WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
            """

            await conn.execute(query, *params)

            # Fetch updated instance
            updated_instance = await conn.fetchrow("""
                SELECT * FROM recurring_expense_instances WHERE id = $1
            """, instance_id)

            # Get attachments
            attachments_data = await conn.fetch("""
                SELECT id, file_name, file_size, mime_type, uploaded_by, uploaded_at, s3_key
                FROM purchase_attachments
                WHERE recurring_instance_id = $1
            """, instance_id)

            s3_service = AWSS3Service()
            attachments = []
            for row in attachments_data:
                att_dict = {
                    'id': str(row['id']),
                    'fileName': row['file_name'],
                    'fileSize': row['file_size'],
                    'mimeType': row['mime_type'],
                    'uploadedBy': str(row['uploaded_by']) if row['uploaded_by'] else None,
                    'uploadedAt': row['uploaded_at'].isoformat() if row['uploaded_at'] else None
                }
                if row['s3_key']:
                    try:
                        presigned_url = await s3_service.get_presigned_url(row['s3_key'], expiration=3600)
                        att_dict['s3Url'] = presigned_url
                    except Exception as e:
                        logger.error(f"Error generating presigned URL: {e}")
                        att_dict['s3Url'] = None
                attachments.append(att_dict)

            return {
                'success': True,
                'data': {
                    'id': str(updated_instance['id']),
                    'tenantId': str(updated_instance['tenant_id']),
                    'expenseId': str(updated_instance['expense_id']),
                    'periodMonth': updated_instance['period_month'],
                    'scheduledDate': updated_instance['scheduled_date'].isoformat(),
                    'amount': float(updated_instance['amount']),
                    'status': updated_instance['status'],
                    'paymentDate': updated_instance['payment_date'].isoformat() if updated_instance['payment_date'] else None,
                    'paymentMethod': updated_instance['payment_method'],
                    'paymentReference': updated_instance['payment_reference'],
                    'notes': updated_instance['notes'],
                    'createdBy': str(updated_instance['created_by']) if updated_instance['created_by'] else None,
                    'createdAt': updated_instance['created_at'].isoformat(),
                    'updatedAt': updated_instance['updated_at'].isoformat(),
                    'attachments': attachments
                }
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating recurring instance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def upload_instance_attachments(
    request: Request,
    response: Response,
    instance_id: UUID,
    files: List[UploadFile]
) -> Dict[str, Any]:
    """
    Upload attachments for a recurring expense instance
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="No files provided")

        async with get_db_connection() as conn:
            # Verify instance exists and belongs to tenant
            instance = await conn.fetchrow("""
                SELECT id, expense_id FROM recurring_expense_instances
                WHERE id = $1 AND tenant_id = $2
            """, instance_id, tenant_id)

            if not instance:
                raise HTTPException(status_code=404, detail="Instance not found")

            # Upload files to S3 and save to database
            s3_service = AWSS3Service()
            uploaded_files = []

            for file in files:
                try:
                    # Upload to S3
                    s3_key = await s3_service.upload_file(
                        file_content=file.file,
                        filename=file.filename,
                        folder='expenses/instance-attachments',
                        content_type=file.content_type
                    )

                    if s3_key:
                        # Generate presigned URL
                        file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)

                        # Save to database
                        attachment_id = await conn.fetchval("""
                            INSERT INTO purchase_attachments (
                                tenant_id,
                                recurring_instance_id,
                                path,
                                file_name,
                                file_size,
                                mime_type,
                                attachment_type,
                                description,
                                uploaded_by,
                                s3_key,
                                s3_url
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            RETURNING id
                        """,
                            tenant_id,
                            instance_id,
                            s3_key,
                            file.filename,
                            file.size or 0,
                            file.content_type or 'application/octet-stream',
                            'payment_proof',
                            f'Comprobante de pago - {file.filename}',
                            user_id,
                            s3_key,
                            file_url
                        )

                        uploaded_files.append({
                            'id': str(attachment_id),
                            'fileName': file.filename,
                            'fileSize': file.size,
                            'mimeType': file.content_type,
                            's3Url': file_url
                        })

                except Exception as e:
                    logger.error(f"Error uploading file {file.filename}: {e}")
                    # Continue with other files

            return {
                'success': True,
                'uploaded': len(uploaded_files),
                'files': uploaded_files
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading instance attachments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def delete_instance_attachment(
    request: Request,
    response: Response,
    attachment_id: UUID
) -> Dict[str, Any]:
    """
    Delete an attachment from a recurring expense instance
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify attachment exists and belongs to tenant
            attachment = await conn.fetchrow("""
                SELECT id, s3_key, recurring_instance_id
                FROM purchase_attachments
                WHERE id = $1 AND tenant_id = $2 AND recurring_instance_id IS NOT NULL
            """, attachment_id, tenant_id)

            if not attachment:
                raise HTTPException(status_code=404, detail="Attachment not found")

            # Delete from S3
            if attachment['s3_key']:
                try:
                    s3_service = AWSS3Service()
                    await s3_service.delete_file(attachment['s3_key'])
                except Exception as e:
                    logger.error(f"Error deleting file from S3: {e}")
                    # Continue with database deletion even if S3 fails

            # Delete from database
            await conn.execute("""
                DELETE FROM purchase_attachments WHERE id = $1
            """, attachment_id)

            return {
                'success': True,
                'message': 'Attachment deleted successfully'
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting instance attachment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")
