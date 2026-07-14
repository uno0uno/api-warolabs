import logging
from decimal import Decimal
from datetime import date
from typing import Optional, List
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import APIError, AuthenticationError, AuthorizationError, ValidationError
from app.models.accounting import (
    TenantAccount,
    TenantAccountCreate,
    TenantAccountUpdate,
    TenantAccountResponse,
    TenantAccountsListResponse,
    AccountRoleBinding,
    AccountRoleBindingsResponse,
    JournalEntryCreate,
    JournalEntry,
    JournalEntryWithLines,
    JournalLine,
    JournalEntryResponse,
    JournalEntriesListResponse,
    TrialBalanceRow,
    TrialBalanceResponse,
    PLRevenue,
    PLCogs,
    PLOperatingExpenses,
    PLProvisions,
    PLPrimeCost,
    PLPeriodData,
    PLStatementResponse,
    ProvisionsBreakdown,
    ProvisionsPreviewResponse,
    ProvisionsPostResponse,
)
from app.services.account_role_service import (
    AccountRole,
    delete_role_override,
    ensure_colombia_payroll,
    list_role_bindings,
    resolve_account,
    set_role_override,
)

logger = logging.getLogger(__name__)

VALID_ACCOUNT_TYPES = ['asset', 'liability', 'equity', 'income', 'expense', 'cogs']
VALID_NORMAL_BALANCES = ['debit', 'credit']


def _derive_level(code: str) -> int:
    """Derive PUC level from code length: 1→class, 2→group, 4→account, 6→sub-account."""
    length = len(code.strip())
    if length == 1:
        return 1
    if length == 2:
        return 2
    if length <= 4:
        return 4
    return 6


async def get_accounts(
    request: Request,
    account_class: Optional[str] = None,
    account_type: Optional[str] = None,
    active: Optional[bool] = None,
) -> TenantAccountsListResponse:
    """
    Return the full chart of accounts for the authenticated tenant.
    Optional filters: class (PUC class digit), type, active status.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        conditions = ["ta.tenant_id = $1"]
        params: list = [tenant_id]

        if account_class is not None:
            params.append(account_class)
            conditions.append(f"ta.account_class = ${len(params)}")

        if account_type is not None:
            params.append(account_type)
            conditions.append(f"ta.account_type = ${len(params)}")

        if active is not None:
            params.append(active)
            conditions.append(f"ta.is_active = ${len(params)}")

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                ta.id,
                ta.tenant_id,
                ta.template_id,
                ta.code,
                ta.name,
                ta.account_class,
                ta.account_type,
                ta.normal_balance,
                ta.level,
                ta.parent_id,
                ta.is_detail,
                ta.is_system,
                ta.is_active,
                ta.created_at
            FROM tenant_accounts ta
            WHERE {where_clause}
            ORDER BY ta.code
        """

        async with get_db_connection() as conn:
            rows = await conn.fetch(query, *params)

        accounts = [
            TenantAccount(
                id=row['id'],
                tenant_id=row['tenant_id'],
                template_id=row['template_id'],
                code=row['code'],
                name=row['name'],
                account_class=row['account_class'],
                account_type=row['account_type'],
                normal_balance=row['normal_balance'],
                level=row['level'],
                parent_id=row['parent_id'],
                is_detail=row['is_detail'],
                is_system=row['is_system'],
                is_active=row['is_active'],
                created_at=row['created_at'],
            )
            for row in rows
        ]

        return TenantAccountsListResponse(data=accounts)

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching accounts: {e}", exc_info=True)
        raise ValidationError("Error al obtener cuentas")


async def get_account_role_bindings(request: Request) -> AccountRoleBindingsResponse:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("No hay un tenant seleccionado")
    async with get_db_connection() as conn:
        rows = await list_role_bindings(conn, tenant_id)
    return AccountRoleBindingsResponse(
        data=[AccountRoleBinding(**row) for row in rows]
    )


async def update_account_role_binding(
    request: Request, role: str, account_id: UUID
) -> AccountRoleBindingsResponse:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("No hay un tenant seleccionado")
    async with get_db_connection() as conn:
        await set_role_override(conn, tenant_id, role.upper(), account_id)
        rows = await list_role_bindings(conn, tenant_id)
    return AccountRoleBindingsResponse(
        data=[AccountRoleBinding(**row) for row in rows]
    )


async def remove_account_role_binding(
    request: Request, role: str
) -> AccountRoleBindingsResponse:
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("No hay un tenant seleccionado")
    async with get_db_connection() as conn:
        await delete_role_override(conn, tenant_id, role.upper())
        rows = await list_role_bindings(conn, tenant_id)
    return AccountRoleBindingsResponse(
        data=[AccountRoleBinding(**row) for row in rows]
    )


async def create_account(request: Request, body: TenantAccountCreate) -> TenantAccountResponse:
    """
    Create a custom account for the tenant.
    Code must be unique per tenant. is_system is always false for API-created accounts.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if body.account_type not in VALID_ACCOUNT_TYPES:
            raise ValidationError(f"Tipo de cuenta inválido. Válidos: {', '.join(VALID_ACCOUNT_TYPES)}")

        if body.normal_balance not in VALID_NORMAL_BALANCES:
            raise ValidationError(f"Saldo normal inválido. Válidos: {', '.join(VALID_NORMAL_BALANCES)}")

        code = body.code.strip()
        if not code:
            raise ValidationError("El código de la cuenta es requerido")

        async with get_db_connection() as conn:
            localization = await conn.fetchval(
                "SELECT accounting_localization FROM tenant_financial_profiles WHERE tenant_id = $1",
                tenant_id,
            )
            level = _derive_level(code) if localization == "WARO_CO_PUC_V1" else body.level
            if level not in (1, 2, 4, 6, 8):
                raise ValidationError("Nivel contable invalido")

            # Ensure code is unique within tenant
            existing = await conn.fetchval(
                "SELECT 1 FROM tenant_accounts WHERE tenant_id = $1 AND code = $2",
                tenant_id, code
            )
            if existing:
                raise ValidationError(f"Ya existe una cuenta con el código '{code}'")

            # Validate parent_id belongs to same tenant
            parent_id = body.parent_id
            if parent_id is not None:
                parent_exists = await conn.fetchval(
                    "SELECT 1 FROM tenant_accounts WHERE id = $1 AND tenant_id = $2",
                    parent_id, tenant_id
                )
                if not parent_exists:
                    raise ValidationError("La cuenta padre no existe o pertenece a otro tenant")

            row = await conn.fetchrow(
                """INSERT INTO tenant_accounts
                       (tenant_id, template_id, code, name, account_class, account_type,
                        normal_balance, level, parent_id, is_detail, is_system, is_active)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, false, true)
                   RETURNING id, tenant_id, template_id, code, name, account_class,
                             account_type, normal_balance, level, parent_id,
                             is_detail, is_system, is_active, created_at""",
                tenant_id,
                body.template_id,
                code,
                body.name,
                body.account_class,
                body.account_type,
                body.normal_balance,
                level,
                parent_id,
                body.is_detail,
            )

        account = TenantAccount(
            id=row['id'],
            tenant_id=row['tenant_id'],
            template_id=row['template_id'],
            code=row['code'],
            name=row['name'],
            account_class=row['account_class'],
            account_type=row['account_type'],
            normal_balance=row['normal_balance'],
            level=row['level'],
            parent_id=row['parent_id'],
            is_detail=row['is_detail'],
            is_system=row['is_system'],
            is_active=row['is_active'],
            created_at=row['created_at'],
        )

        logger.info(f"✅ Account created: {code} for tenant {tenant_id}")
        return TenantAccountResponse(data=account)

    except (APIError, AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error creating account: {e}", exc_info=True)
        raise ValidationError("Error al crear cuenta")


async def update_account(
    request: Request, account_id: UUID, body: TenantAccountUpdate
) -> TenantAccountResponse:
    """
    Update name, active status, or is_detail flag.
    System accounts can be deactivated but not renamed.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            current = await conn.fetchrow(
                """SELECT id, is_system, is_active, name, is_detail
                   FROM tenant_accounts WHERE id = $1 AND tenant_id = $2""",
                account_id, tenant_id
            )
            if not current:
                raise ValidationError("Cuenta no encontrada")

            # System accounts cannot be renamed
            if current['is_system'] and body.name is not None and body.name != current['name']:
                raise AuthorizationError("No se puede renombrar una cuenta del sistema")

            # Build SET clause dynamically
            updates: list = []
            params: list = []

            if body.name is not None:
                params.append(body.name)
                updates.append(f"name = ${len(params)}")

            if body.is_active is not None:
                params.append(body.is_active)
                updates.append(f"is_active = ${len(params)}")

            if body.is_detail is not None:
                params.append(body.is_detail)
                updates.append(f"is_detail = ${len(params)}")

            if not updates:
                raise ValidationError("No hay campos para actualizar")

            params.append(account_id)
            params.append(tenant_id)
            set_clause = ", ".join(updates)

            row = await conn.fetchrow(
                f"""UPDATE tenant_accounts SET {set_clause}
                    WHERE id = ${len(params) - 1} AND tenant_id = ${len(params)}
                    RETURNING id, tenant_id, template_id, code, name, account_class,
                              account_type, normal_balance, level, parent_id,
                              is_detail, is_system, is_active, created_at""",
                *params
            )

        account = TenantAccount(
            id=row['id'],
            tenant_id=row['tenant_id'],
            template_id=row['template_id'],
            code=row['code'],
            name=row['name'],
            account_class=row['account_class'],
            account_type=row['account_type'],
            normal_balance=row['normal_balance'],
            level=row['level'],
            parent_id=row['parent_id'],
            is_detail=row['is_detail'],
            is_system=row['is_system'],
            is_active=row['is_active'],
            created_at=row['created_at'],
        )

        logger.info(f"✏️ Account updated: {account_id} for tenant {tenant_id}")
        return TenantAccountResponse(data=account)

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error updating account: {e}", exc_info=True)
        raise ValidationError("Error al actualizar cuenta")


async def delete_account(request: Request, account_id: UUID) -> dict:
    """
    Soft-delete (deactivate) a custom account.
    System accounts and accounts with journal lines cannot be deleted.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            account = await conn.fetchrow(
                "SELECT id, code, name, is_system FROM tenant_accounts WHERE id = $1 AND tenant_id = $2",
                account_id, tenant_id
            )
            if not account:
                raise ValidationError("Cuenta no encontrada")

            if account['is_system']:
                raise AuthorizationError("No se puede eliminar una cuenta del sistema")

            # Guard: block if account has journal lines
            has_lines = await conn.fetchval(
                "SELECT 1 FROM tenant_journal_lines WHERE account_id = $1 LIMIT 1",
                account_id
            )
            if has_lines:
                raise ValidationError(
                    "No se puede eliminar una cuenta con movimientos contables. Use desactivar."
                )

            await conn.execute(
                "UPDATE tenant_accounts SET is_active = false WHERE id = $1 AND tenant_id = $2",
                account_id, tenant_id
            )

        logger.info(f"🗑️ Account soft-deleted: {account['code']} for tenant {tenant_id}")
        return {"success": True, "message": f"Cuenta '{account['name']}' desactivada"}

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error deleting account: {e}", exc_info=True)
        raise ValidationError("Error al eliminar cuenta")


# ---------------------------------------------------------------------------
# Journal Entries (#376)
# ---------------------------------------------------------------------------

def _row_to_journal_entry(row) -> JournalEntry:
    # asyncpg Record allows .get() via dict-cast; fall back when the column
    # was not selected (some legacy SELECTs may not include pending_review).
    pending_review = False
    try:
        pending_review = bool(row['pending_review'])
    except (KeyError, IndexError):
        pending_review = False

    return JournalEntry(
        id=row['id'],
        tenant_id=row['tenant_id'],
        entry_date=str(row['entry_date']),
        period_year=row['period_year'],
        period_month=row['period_month'],
        description=row['description'],
        reference=row['reference'],
        source_module=row['source_module'],
        source_id=row['source_id'],
        status=row['status'],
        total_debit=float(row['total_debit']),
        total_credit=float(row['total_credit']),
        created_by=row['created_by'],
        posted_at=row['posted_at'],
        voided_at=row['voided_at'],
        created_at=row['created_at'],
        pending_review=pending_review,
    )


def _row_to_journal_line(row) -> JournalLine:
    return JournalLine(
        id=row['id'],
        journal_entry_id=row['journal_entry_id'],
        account_id=row['account_id'],
        debit=float(row['debit']),
        credit=float(row['credit']),
        description=row['description'],
        line_order=row['line_order'],
        created_at=row['created_at'],
    )


async def _assert_period_open(conn, tenant_id: UUID, year: int, month: int) -> None:
    """Raises AuthorizationError if the monthly period is closed."""
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, year, month,
    )
    if closed:
        raise AuthorizationError(
            f"No se puede modificar un asiento en el período {year}-{month:02d} (período cerrado)"
        )


async def void_order_journal_entry_in_txn(
    conn,
    tenant_id: UUID,
    order_id: UUID,
    user_id: Optional[UUID],
    reason: str,
) -> Optional[UUID]:
    """
    Issue warocol.com#649 — void the posted sale entry for an order and post
    a balancing reversing entry, all inside the caller's transaction.

    Mirrors `void_journal_entry` semantics but works against an already-open
    connection so a single transaction covers payment-void + GL-void atomically.

    Returns the id of the reversing entry, or None when no posted entry exists
    for this order (early-stage partials never created one — nothing to undo).
    """
    entry_row = await conn.fetchrow(
        """SELECT id, entry_date, period_year, period_month, description,
                  reference, total_debit, total_credit, status
           FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'orden' AND source_id = $2
             AND status = 'posted'
           ORDER BY created_at DESC
           LIMIT 1
           FOR UPDATE""",
        tenant_id, order_id,
    )
    if not entry_row:
        return None

    await _assert_period_open(conn, tenant_id, entry_row['period_year'], entry_row['period_month'])

    original_lines = await conn.fetch(
        """SELECT account_id, debit, credit, description, line_order
           FROM tenant_journal_lines WHERE journal_entry_id = $1
           ORDER BY line_order""",
        entry_row['id'],
    )

    await conn.execute(
        """UPDATE tenant_journal_entries
           SET status = 'voided', voided_at = NOW()
           WHERE id = $1""",
        entry_row['id'],
    )

    rev_description = f"Reversión: {entry_row['description']} — {reason.strip()}"
    rev_row = await conn.fetchrow(
        """INSERT INTO tenant_journal_entries
               (tenant_id, entry_date, period_year, period_month,
                description, reference, source_module, source_id,
                status, total_debit, total_credit, created_by, posted_at)
           VALUES ($1, $2, $3, $4, $5, $6, 'system', $7,
                   'posted', $8, $9, $10, NOW())
           RETURNING id""",
        tenant_id,
        entry_row['entry_date'],
        entry_row['period_year'],
        entry_row['period_month'],
        rev_description,
        entry_row['reference'],
        entry_row['id'],
        float(entry_row['total_credit']),
        float(entry_row['total_debit']),
        user_id,
    )

    for line in original_lines:
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit,
                    description, line_order)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            rev_row['id'],
            line['account_id'],
            float(line['credit']),
            float(line['debit']),
            line['description'],
            line['line_order'],
        )

    logger.info(
        f"🔄 Order GL entry voided in payment-void txn: order={order_id} entry={entry_row['id']} → reversing {rev_row['id']}"
    )
    return rev_row['id']


async def create_journal_entry(request: Request, body: JournalEntryCreate) -> JournalEntryResponse:
    """
    Create a draft journal entry with its lines.
    All account_ids must belong to the tenant.
    source_module defaults to 'manual'.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if not body.lines:
            raise ValidationError("El asiento debe tener al menos una línea")

        entry_date_obj = date.fromisoformat(body.entry_date)
        period_year = entry_date_obj.year
        period_month = entry_date_obj.month

        async with get_db_connection() as conn:
            # Validate all account_ids belong to this tenant
            account_ids = list({str(line.account_id) for line in body.lines})
            valid_count = await conn.fetchval(
                """SELECT COUNT(*) FROM tenant_accounts
                   WHERE id = ANY($1::uuid[]) AND tenant_id = $2""",
                account_ids, tenant_id,
            )
            if valid_count != len(account_ids):
                raise ValidationError("Una o más cuentas no pertenecen al tenant actual")

            async with conn.transaction():
                # Issue #531 — accept caller-supplied source_module/source_id
                # (e.g. 'manual_balance_adjustment' for the "Actualizar saldo
                # real" flow) and the pending_review annotation flag.
                source_module = body.source_module or 'manual'
                source_id = body.source_id
                pending_review = bool(body.pending_review)

                entry_row = await conn.fetchrow(
                    """INSERT INTO tenant_journal_entries
                           (tenant_id, entry_date, period_year, period_month,
                            description, reference, source_module, source_id,
                            status, created_by, pending_review)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'draft', $9, $10)
                       RETURNING id, tenant_id, entry_date, period_year, period_month,
                                 description, reference, source_module, source_id,
                                 status, total_debit, total_credit, created_by,
                                 posted_at, voided_at, created_at, pending_review""",
                    tenant_id, entry_date_obj, period_year, period_month,
                    body.description, body.reference, source_module, source_id,
                    user_id, pending_review,
                )
                entry_id = entry_row['id']

                line_rows = []
                for i, line in enumerate(body.lines):
                    lr = await conn.fetchrow(
                        """INSERT INTO tenant_journal_lines
                               (journal_entry_id, account_id, debit, credit,
                                description, line_order)
                           VALUES ($1, $2, $3, $4, $5, $6)
                           RETURNING id, journal_entry_id, account_id, debit, credit,
                                     description, line_order, created_at""",
                        entry_id, line.account_id,
                        line.debit, line.credit,
                        line.description, line.line_order if line.line_order else i,
                    )
                    line_rows.append(lr)

        entry = _row_to_journal_entry(entry_row)
        lines = [_row_to_journal_line(r) for r in line_rows]
        entry_with_lines = JournalEntryWithLines(**entry.dict(), lines=lines)

        logger.info(f"✅ Journal entry created (draft): {entry_id} for tenant {tenant_id}")
        return JournalEntryResponse(data=entry_with_lines)

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error creating journal entry: {e}", exc_info=True)
        raise ValidationError("Error al crear asiento contable")


async def list_journal_entries(
    request: Request,
    status: Optional[str] = None,
    source_module: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    account_id: Optional[UUID] = None,
    page: int = 1,
    limit: int = 50,
) -> JournalEntriesListResponse:
    """
    Paginated list of journal entries for the tenant.
    Optional filters: status, source_module, date_from, date_to, account_id.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        conditions = ["je.tenant_id = $1"]
        params: list = [tenant_id]

        if status:
            params.append(status)
            conditions.append(f"je.status = ${len(params)}")

        if source_module:
            params.append(source_module)
            conditions.append(f"je.source_module = ${len(params)}")

        if date_from:
            params.append(date.fromisoformat(date_from))
            conditions.append(f"je.entry_date >= ${len(params)}")

        if date_to:
            params.append(date.fromisoformat(date_to))
            conditions.append(f"je.entry_date <= ${len(params)}")

        if account_id is not None:
            params.append(account_id)
            conditions.append(
                f"EXISTS (SELECT 1 FROM tenant_journal_lines jl "
                f"WHERE jl.journal_entry_id = je.id AND jl.account_id = ${len(params)})"
            )

        where_clause = " AND ".join(conditions)
        if account_id is not None:
            # Ledger view: return line-level debit/credit for the specific account,
            # not the entry totals (which reflect the full multi-account entry).
            base_query = f"""
                SELECT je.id, je.tenant_id, je.entry_date, je.period_year, je.period_month,
                       je.description, je.reference, je.source_module, je.source_id,
                       je.status, jl.debit AS total_debit, jl.credit AS total_credit,
                       je.created_by, je.posted_at, je.voided_at, je.created_at,
                       je.pending_review
                FROM tenant_journal_entries je
                JOIN tenant_journal_lines jl
                  ON jl.journal_entry_id = je.id AND jl.account_id = ${len(params)}
                WHERE {where_clause}
            """
        else:
            base_query = f"""
                SELECT je.id, je.tenant_id, je.entry_date, je.period_year, je.period_month,
                       je.description, je.reference, je.source_module, je.source_id,
                       je.status, je.total_debit, je.total_credit, je.created_by,
                       je.posted_at, je.voided_at, je.created_at, je.pending_review
                FROM tenant_journal_entries je
                WHERE {where_clause}
            """

        async with get_db_connection() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM tenant_journal_entries je WHERE {where_clause}",
                *params,
            )

            # Opening balance: net of all POSTED lines for this account before date_from
            # Sign depends on account normal_balance:
            #   debit-normal  (Activos, Gastos, Costos): net = debit - credit
            #   credit-normal (Pasivos, Patrimonio, Ingresos): net = credit - debit
            opening_balance: Optional[float] = None
            if account_id is not None:
                acct_row = await conn.fetchrow(
                    "SELECT normal_balance FROM tenant_accounts WHERE id = $1",
                    account_id,
                )
                normal_balance = acct_row["normal_balance"] if acct_row else "debit"

                ob_params: list = [tenant_id, account_id]
                ob_date_cond = ""
                if date_from:
                    ob_params.append(date.fromisoformat(date_from))
                    ob_date_cond = f"AND je.entry_date < ${len(ob_params)}"

                if normal_balance == "debit":
                    net_formula = "COALESCE(SUM(jl.debit), 0) - COALESCE(SUM(jl.credit), 0)"
                else:
                    net_formula = "COALESCE(SUM(jl.credit), 0) - COALESCE(SUM(jl.debit), 0)"

                ob_row = await conn.fetchrow(
                    f"""SELECT {net_formula} AS net
                        FROM tenant_journal_lines jl
                        JOIN tenant_journal_entries je ON je.id = jl.journal_entry_id
                        WHERE je.tenant_id = $1
                          AND jl.account_id = $2
                          AND je.status = 'posted'
                          {ob_date_cond}""",
                    *ob_params,
                )
                opening_balance = float(ob_row["net"]) if ob_row else 0.0

            offset = (page - 1) * limit
            params.append(limit)
            params.append(offset)
            # Ledger view (account_id filter): sort ASC so running balance accumulates naturally
            sort_order = "ASC" if account_id is not None else "DESC"
            rows = await conn.fetch(
                base_query + f" ORDER BY je.entry_date {sort_order}, je.created_at {sort_order} "
                             f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        entries = [_row_to_journal_entry(r) for r in rows]
        return JournalEntriesListResponse(data=entries, total=total, opening_balance=opening_balance)

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error listing journal entries: {e}", exc_info=True)
        raise ValidationError("Error al listar asientos contables")


async def get_journal_entry(request: Request, entry_id: UUID) -> JournalEntryResponse:
    """Return a single journal entry with all its lines."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            entry_row = await conn.fetchrow(
                """SELECT id, tenant_id, entry_date, period_year, period_month,
                          description, reference, source_module, source_id,
                          status, total_debit, total_credit, created_by,
                          posted_at, voided_at, created_at, pending_review
                   FROM tenant_journal_entries
                   WHERE id = $1 AND tenant_id = $2""",
                entry_id, tenant_id,
            )
            if not entry_row:
                raise ValidationError("Asiento no encontrado")

            line_rows = await conn.fetch(
                """SELECT id, journal_entry_id, account_id, debit, credit,
                          description, line_order, created_at
                   FROM tenant_journal_lines
                   WHERE journal_entry_id = $1
                   ORDER BY line_order, created_at""",
                entry_id,
            )

        entry = _row_to_journal_entry(entry_row)
        lines = [_row_to_journal_line(r) for r in line_rows]
        entry_with_lines = JournalEntryWithLines(**entry.dict(), lines=lines)
        return JournalEntryResponse(data=entry_with_lines)

    except (AuthenticationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching journal entry: {e}", exc_info=True)
        raise ValidationError("Error al obtener asiento contable")


async def post_journal_entry(request: Request, entry_id: UUID) -> JournalEntryResponse:
    """
    Post a draft journal entry to the GL.
    Validates: status must be draft, period must be open, debits == credits.
    Sets total_debit / total_credit on the header.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        async with get_db_connection() as conn:
            entry_row = await conn.fetchrow(
                """SELECT id, status, period_year, period_month
                   FROM tenant_journal_entries WHERE id = $1 AND tenant_id = $2""",
                entry_id, tenant_id,
            )
            if not entry_row:
                raise ValidationError("Asiento no encontrado")

            if entry_row['status'] != 'draft':
                raise ValidationError(
                    f"Solo se pueden publicar asientos en borrador (estado actual: {entry_row['status']})"
                )

            await _assert_period_open(conn, tenant_id, entry_row['period_year'], entry_row['period_month'])

            line_rows = await conn.fetch(
                "SELECT debit, credit FROM tenant_journal_lines WHERE journal_entry_id = $1",
                entry_id,
            )
            if not line_rows:
                raise ValidationError("El asiento no tiene líneas")

            total_debit = sum(Decimal(str(r['debit'])) for r in line_rows)
            total_credit = sum(Decimal(str(r['credit'])) for r in line_rows)

            if total_debit != total_credit:
                raise ValidationError(
                    f"El asiento no está balanceado: débitos {total_debit} ≠ créditos {total_credit}"
                )

            async with conn.transaction():
                updated = await conn.fetchrow(
                    """UPDATE tenant_journal_entries
                       SET status = 'posted', posted_at = NOW(),
                           total_debit = $2, total_credit = $3
                       WHERE id = $1 AND tenant_id = $4
                       RETURNING id, tenant_id, entry_date, period_year, period_month,
                                 description, reference, source_module, source_id,
                                 status, total_debit, total_credit, created_by,
                                 posted_at, voided_at, created_at, pending_review""",
                    entry_id, float(total_debit), float(total_credit), tenant_id,
                )

            full_lines = await conn.fetch(
                """SELECT id, journal_entry_id, account_id, debit, credit,
                          description, line_order, created_at
                   FROM tenant_journal_lines WHERE journal_entry_id = $1
                   ORDER BY line_order""",
                entry_id,
            )

        entry = _row_to_journal_entry(updated)
        lines = [_row_to_journal_line(r) for r in full_lines]
        entry_with_lines = JournalEntryWithLines(**entry.dict(), lines=lines)

        logger.info(f"📒 Journal entry posted: {entry_id} for tenant {tenant_id}")
        return JournalEntryResponse(data=entry_with_lines)

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error posting journal entry: {e}", exc_info=True)
        raise ValidationError("Error al publicar asiento contable")


async def void_journal_entry(
    request: Request, entry_id: UUID, reason: str
) -> JournalEntryResponse:
    """
    Void a posted entry and auto-create a reversing entry (lines swapped, auto-posted).
    The reversing entry is created in the same transaction.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if not reason or not reason.strip():
            raise ValidationError("Se requiere un motivo para anular el asiento")

        async with get_db_connection() as conn:
            entry_row = await conn.fetchrow(
                """SELECT id, entry_date, period_year, period_month, description,
                          reference, total_debit, total_credit, status
                   FROM tenant_journal_entries WHERE id = $1 AND tenant_id = $2""",
                entry_id, tenant_id,
            )
            if not entry_row:
                raise ValidationError("Asiento no encontrado")

            if entry_row['status'] != 'posted':
                raise ValidationError(
                    f"Solo se pueden anular asientos publicados (estado actual: {entry_row['status']})"
                )

            await _assert_period_open(conn, tenant_id, entry_row['period_year'], entry_row['period_month'])

            original_lines = await conn.fetch(
                """SELECT account_id, debit, credit, description, line_order
                   FROM tenant_journal_lines WHERE journal_entry_id = $1
                   ORDER BY line_order""",
                entry_id,
            )
            if not original_lines:
                raise ValidationError("El asiento no tiene líneas")

            async with conn.transaction():
                # 1. Mark original as voided
                voided_row = await conn.fetchrow(
                    """UPDATE tenant_journal_entries
                       SET status = 'voided', voided_at = NOW()
                       WHERE id = $1 AND tenant_id = $2
                       RETURNING id, tenant_id, entry_date, period_year, period_month,
                                 description, reference, source_module, source_id,
                                 status, total_debit, total_credit, created_by,
                                 posted_at, voided_at, created_at, pending_review""",
                    entry_id, tenant_id,
                )

                # 2. Create reversing entry header (auto-posted)
                rev_description = f"Reversión: {entry_row['description']} — {reason.strip()}"
                rev_row = await conn.fetchrow(
                    """INSERT INTO tenant_journal_entries
                           (tenant_id, entry_date, period_year, period_month,
                            description, reference, source_module, source_id,
                            status, total_debit, total_credit, created_by, posted_at)
                       VALUES ($1, $2, $3, $4, $5, $6, 'system', $7,
                               'posted', $8, $9, $10, NOW())
                       RETURNING id, tenant_id, entry_date, period_year, period_month,
                                 description, reference, source_module, source_id,
                                 status, total_debit, total_credit, created_by,
                                 posted_at, voided_at, created_at, pending_review""",
                    tenant_id,
                    entry_row['entry_date'],
                    entry_row['period_year'],
                    entry_row['period_month'],
                    rev_description,
                    entry_row['reference'],
                    entry_id,          # source_id = original entry id
                    float(entry_row['total_credit']),   # swapped
                    float(entry_row['total_debit']),    # swapped
                    user_id,
                )
                rev_entry_id = rev_row['id']

                # 3. Insert reversed lines (debit ↔ credit)
                rev_line_rows = []
                for line in original_lines:
                    rl = await conn.fetchrow(
                        """INSERT INTO tenant_journal_lines
                               (journal_entry_id, account_id, debit, credit,
                                description, line_order)
                           VALUES ($1, $2, $3, $4, $5, $6)
                           RETURNING id, journal_entry_id, account_id, debit, credit,
                                     description, line_order, created_at""",
                        rev_entry_id,
                        line['account_id'],
                        float(line['credit']),   # swapped
                        float(line['debit']),    # swapped
                        line['description'],
                        line['line_order'],
                    )
                    rev_line_rows.append(rl)

        rev_entry = _row_to_journal_entry(rev_row)
        rev_lines = [_row_to_journal_line(r) for r in rev_line_rows]
        rev_with_lines = JournalEntryWithLines(**rev_entry.dict(), lines=rev_lines)

        logger.info(
            f"🔄 Journal entry voided: {entry_id} → reversing entry {rev_entry_id} for tenant {tenant_id}"
        )
        return JournalEntryResponse(data=rev_with_lines)

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error voiding journal entry: {e}", exc_info=True)
        raise ValidationError("Error al anular asiento contable")


# --- Trial Balance (#379) ---

async def get_trial_balance(
    request: Request,
    period_start: str,
    period_end: str,
    include_zero_balances: bool = False,
) -> TrialBalanceResponse:
    """
    Compute the trial balance for the authenticated tenant.

    opening_balance = sum of all posted lines BEFORE period_start
    period_debits / period_credits = posted lines within [period_start, period_end]
    closing_balance = opening ± period net  (sign depends on normal_balance)

    A single SQL pass uses FILTER (WHERE ...) aggregates so we hit the index once.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        start_date = date.fromisoformat(period_start)
        end_date   = date.fromisoformat(period_end)

        async with get_db_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ta.id                AS account_id,
                    ta.code,
                    ta.name,
                    ta.account_class,
                    ta.account_type,
                    ta.normal_balance,
                    -- Opening: all POSTED lines BEFORE period_start
                    COALESCE(SUM(jl.debit)  FILTER (
                        WHERE je.status = 'posted'
                          AND je.entry_date < $2
                    ), 0)                AS open_debits,
                    COALESCE(SUM(jl.credit) FILTER (
                        WHERE je.status = 'posted'
                          AND je.entry_date < $2
                    ), 0)                AS open_credits,
                    -- Period: POSTED lines within [period_start, period_end]
                    COALESCE(SUM(jl.debit)  FILTER (
                        WHERE je.status = 'posted'
                          AND je.entry_date >= $2
                          AND je.entry_date <= $3
                    ), 0)                AS period_debits,
                    COALESCE(SUM(jl.credit) FILTER (
                        WHERE je.status = 'posted'
                          AND je.entry_date >= $2
                          AND je.entry_date <= $3
                    ), 0)                AS period_credits
                FROM tenant_accounts ta
                LEFT JOIN tenant_journal_lines jl ON jl.account_id = ta.id
                LEFT JOIN tenant_journal_entries je ON je.id = jl.journal_entry_id
                                                    AND je.tenant_id = $1
                WHERE ta.tenant_id = $1
                  AND ta.is_active = TRUE
                  AND ta.is_detail = TRUE
                GROUP BY ta.id, ta.code, ta.name, ta.account_class,
                         ta.account_type, ta.normal_balance
                ORDER BY ta.code
                """,
                tenant_id,
                start_date,
                end_date,
            )

        result_rows: List[TrialBalanceRow] = []
        total_debits = Decimal('0')
        total_credits = Decimal('0')

        for r in rows:
            open_deb = Decimal(str(r['open_debits']))
            open_cre = Decimal(str(r['open_credits']))
            p_deb    = Decimal(str(r['period_debits']))
            p_cre    = Decimal(str(r['period_credits']))

            # Opening balance — depends on which side is "normal"
            if r['normal_balance'] == 'debit':
                opening = open_deb - open_cre
                closing = opening + p_deb - p_cre
            else:
                opening = open_cre - open_deb
                closing = opening + p_cre - p_deb

            # Skip fully-zero accounts unless caller wants them
            if not include_zero_balances:
                if opening == 0 and p_deb == 0 and p_cre == 0 and closing == 0:
                    continue

            total_debits  += p_deb
            total_credits += p_cre

            result_rows.append(TrialBalanceRow(**{
                'accountId':     str(r['account_id']),
                'code':          r['code'],
                'name':          r['name'],
                'class':         r['account_class'],
                'accountType':   r['account_type'],
                'normalBalance': r['normal_balance'],
                'openingBalance': float(opening),
                'periodDebits':   float(p_deb),
                'periodCredits':  float(p_cre),
                'closingBalance': float(closing),
            }))

        is_balanced = abs(total_debits - total_credits) < Decimal('0.01')

        logger.info(
            f"📊 Trial balance computed for tenant {tenant_id}: "
            f"{len(result_rows)} accounts, balanced={is_balanced}"
        )

        return TrialBalanceResponse(**{
            'periodStart':  period_start,
            'periodEnd':    period_end,
            'rows':         result_rows,
            'totalDebits':  float(total_debits),
            'totalCredits': float(total_credits),
            'isBalanced':   is_balanced,
        })

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error computing trial balance: {e}", exc_info=True)
        raise ValidationError("Error al calcular balance de comprobación")


# --- P&L Statement (#383) ---

# Colombian law provision rates (Decreto 2663/1950 — Código Sustantivo del Trabajo)
_CESANTIAS_RATE      = Decimal('0.0833')   # 1/12 of annual salary
_PRIMA_RATE          = Decimal('0.0833')   # 1/12
_VACACIONES_RATE     = Decimal('0.0417')   # 15 days/year ÷ 12
_INTERESES_CES_RATE  = Decimal('0.0100')   # 12% annual ÷ 12

# Expense category codes → P&L operating expense bucket
_OPEX_CATEGORY_MAP = {
    'RENT':         'rent',
    'UTILITIES':    'utilities',
    'MAINTENANCE':  'maintenance',
}
# All other categories fall into 'other'

_PRIME_COST_BENCHMARK = Decimal('65.0')


async def _compute_pl_for_period(
    conn,
    tenant_id: UUID,
    year: int,
    month: int,
    include_colombia_payroll: bool = True,
) -> PLPeriodData:
    """
    Compute the full P&L for a single calendar month.
    All monetary values in Decimal; converted to float at return time.

    Revenue  = SUM(closing_summary.total_sales) for non-deleted cierres in the month
    COGS     = SUM(tenant_expenses.amount WHERE expense_type = 'cogs') in month_year
               + SUM(tenant_purchases.total_amount WHERE received in month) [if any]
    Opex     = SUM(tenant_expenses.amount) grouped by expense_categories.category_code
    Payroll  = SUM(salary_payments.payment_amount WHERE status='paid') in period_month
    Prov base= SUM(latest employee_salaries.total_salary) for active employees
    """
    month_str = f"{year}-{month:02d}"
    month_start = date(year, month, 1)
    # Exclusive upper bound: first day of next month.
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    # --- Revenue ---
    rev_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(cs.total_sales), 0) AS total_sales
        FROM closing_summary cs
        JOIN accounting_period ap ON ap.id = cs.accounting_period_id
        WHERE ap.tenant_id = $1
          AND ap.period_start >= $2
          AND ap.period_start <  $3
          AND ap.deleted_at IS NULL
        """,
        tenant_id, month_start, month_end,
    )
    revenue = Decimal(str(rev_row['total_sales']))

    # --- COGS from expenses (expense_type = 'cogs') ---
    cogs_exp_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(te.amount), 0) AS total
        FROM tenant_expenses te
        WHERE te.tenant_id = $1
          AND te.month_year = $2
          AND te.expense_type = 'cogs'
        """,
        tenant_id, month_str,
    )
    cogs_from_expenses = Decimal(str(cogs_exp_row['total']))

    # --- COGS from received purchases ---
    cogs_purch_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(tp.total_amount), 0) AS total
        FROM tenant_purchases tp
        WHERE tp.tenant_id = $1
          AND tp.received_at >= $2::date::timestamptz
          AND tp.received_at <  $3::date::timestamptz
          AND tp.status IN ('received', 'verified', 'invoiced', 'paid')
        """,
        tenant_id, month_start, month_end,
    )
    cogs_from_purchases = Decimal(str(cogs_purch_row['total']))

    food_cost = cogs_from_expenses + cogs_from_purchases

    # --- Operating expenses by category ---
    opex_rows = await conn.fetch(
        """
        SELECT ec.category_code, COALESCE(SUM(te.amount), 0) AS total
        FROM tenant_expenses te
        JOIN expense_categories ec ON ec.id = te.expense_category_id
        WHERE te.tenant_id = $1
          AND te.month_year = $2
          AND (te.expense_type IS NULL OR te.expense_type != 'cogs')
        GROUP BY ec.category_code
        """,
        tenant_id, month_str,
    )

    opex_buckets: dict = {'rent': Decimal('0'), 'utilities': Decimal('0'),
                          'maintenance': Decimal('0'), 'other': Decimal('0')}
    for row in opex_rows:
        bucket = _OPEX_CATEGORY_MAP.get(row['category_code'], 'other')
        opex_buckets[bucket] += Decimal(str(row['total']))

    payroll = Decimal('0')
    prov_base = Decimal('0')
    if include_colombia_payroll:
        payroll_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(sp.payment_amount), 0) AS total
            FROM salary_payments sp
            JOIN tenant_members tm ON tm.id = sp.tenant_member_id
            WHERE tm.tenant_id = $1
              AND sp.period_month = $2
              AND sp.status = 'paid'
            """,
            tenant_id, month_str,
        )
        payroll = Decimal(str(payroll_row['total']))

        # Latest configured salary per employee at or before the target month.
        prov_base_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(es.total_salary), 0) AS total
            FROM employee_salaries es
            JOIN tenant_members tm ON tm.id = es.tenant_member_id
            WHERE tm.tenant_id = $1
              AND es.period_month = (
                  SELECT MAX(es2.period_month)
                  FROM employee_salaries es2
                  WHERE es2.tenant_member_id = es.tenant_member_id
                    AND es2.period_month <= $2
              )
            """,
            tenant_id, month_str,
        )
        prov_base = Decimal(str(prov_base_row['total']))

    # --- Calculations ---
    cesantias     = (prov_base * _CESANTIAS_RATE).quantize(Decimal('1'))
    prima         = (prov_base * _PRIMA_RATE).quantize(Decimal('1'))
    vacaciones    = (prov_base * _VACACIONES_RATE).quantize(Decimal('1'))
    intereses_ces = (prov_base * _INTERESES_CES_RATE).quantize(Decimal('1'))
    provisions_total = cesantias + prima + vacaciones + intereses_ces

    opex_total = sum(opex_buckets.values())
    # Add payroll into opex for EBITDA calc (payroll is an operating expense)
    gross_profit    = revenue - food_cost
    ebitda          = gross_profit - opex_total - payroll
    net_income      = ebitda - provisions_total

    def _pct(num: Decimal, den: Decimal) -> float:
        if den == 0:
            return 0.0
        return float((num / den * 100).quantize(Decimal('0.1')))

    gross_margin_pct  = _pct(gross_profit, revenue)
    ebitda_margin_pct = _pct(ebitda, revenue)
    food_cost_pct     = _pct(food_cost, revenue)
    labor_pct         = _pct(payroll, revenue)
    prime_cost_pct    = food_cost_pct + labor_pct
    prime_status      = 'ok' if Decimal(str(prime_cost_pct)) <= _PRIME_COST_BENCHMARK else 'warning'

    return PLPeriodData(**{
        'period': month_str,
        'revenue': PLRevenue(**{
            'foodBeverageSales': float(revenue),
            'total':             float(revenue),
        }),
        'cogs': PLCogs(**{
            'foodCost': float(food_cost),
            'total':    float(food_cost),
        }),
        'grossProfit':      float(gross_profit),
        'grossMarginPct':   gross_margin_pct,
        'operatingExpenses': PLOperatingExpenses(**{
            'payroll':      float(payroll),
            'rent':         float(opex_buckets['rent']),
            'utilities':    float(opex_buckets['utilities']),
            'maintenance':  float(opex_buckets['maintenance']),
            'other':        float(opex_buckets['other']),
            'total':        float(opex_total + payroll),
        }),
        'ebitda':           float(ebitda),
        'ebitdaMarginPct':  ebitda_margin_pct,
        'provisions': PLProvisions(**{
            'cesantias':          float(cesantias),
            'prima':              float(prima),
            'vacaciones':         float(vacaciones),
            'interesesCesantias': float(intereses_ces),
            'total':              float(provisions_total),
        }),
        'netIncome': float(net_income),
        'primeCost': PLPrimeCost(**{
            'foodCostPct':   food_cost_pct,
            'laborPct':      labor_pct,
            'totalPct':      prime_cost_pct,
            'benchmarkPct':  65.0,
            'status':        prime_status,
        }),
    })


async def get_pl_statement(
    request: Request,
    year: int,
    month: int,
    compare_previous: bool = False,
) -> PLStatementResponse:
    """
    Monthly P&L statement for the authenticated tenant.
    If compare_previous=True, also computes the prior calendar month.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if not (1 <= month <= 12):
            raise ValidationError("El mes debe estar entre 1 y 12")

        async with get_db_connection() as conn:
            profile = await conn.fetchrow(
                """
                SELECT country_code, base_currency_code, accounting_localization
                FROM tenant_financial_profiles WHERE tenant_id = $1
                """,
                tenant_id,
            )
            if not profile:
                raise ValidationError("Perfil financiero no configurado")
            include_colombia_payroll = (
                profile['country_code'] == 'CO'
                and profile['accounting_localization'] == 'WARO_CO_PUC_V1'
            )
            current = await _compute_pl_for_period(
                conn, tenant_id, year, month, include_colombia_payroll
            )

            previous = None
            if compare_previous:
                prev_month = month - 1 if month > 1 else 12
                prev_year  = year if month > 1 else year - 1
                previous = await _compute_pl_for_period(
                    conn, tenant_id, prev_year, prev_month, include_colombia_payroll
                )

        logger.info(
            f"📊 P&L statement computed for tenant {tenant_id}: "
            f"{year}-{month:02d}, compare_previous={compare_previous}"
        )
        return PLStatementResponse(**{
            'baseCurrencyCode': profile['base_currency_code'],
            'accountingLocalization': profile['accounting_localization'],
            'current': current,
            'previous': previous,
        })

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error computing P&L statement: {e}", exc_info=True)
        raise ValidationError("Error al calcular estado de resultados")


# --- Provisions (#384) ---

# Colombian law 2026 constants (update annually)
_SMMLV_2026            = Decimal('1423500')
_AUXILIO_TRANSPORT_2026 = Decimal('200650')
_SMMLV_TRANSPORT_THRESHOLD = _SMMLV_2026 * 2  # 2,847,000

# Provision rates (Código Sustantivo del Trabajo)
_PROV_CESANTIAS   = Decimal('0.0833')
_PROV_INTERESES   = Decimal('0.0100')
_PROV_PRIMA       = Decimal('0.0833')
_PROV_VACACIONES  = Decimal('0.0417')

async def _calculate_provisions(conn, tenant_id: UUID, year: int, month: int) -> dict:
    """
    Compute provision amounts for the given calendar month.

    Payroll base  = latest total_salary per active employee (period_month ≤ YYYY-MM)
    Transport base = payroll_base + auxilio_transporte for employees earning ≤ 2×SMMLV
                    (used for cesantías, intereses, prima — NOT vacaciones)
    Vacation base  = payroll_base only (per Corte Suprema de Justicia)

    Returns a dict with all bases and provision amounts as Decimal.
    """
    month_str = f"{year}-{month:02d}"

    rows = await conn.fetch(
        """
        SELECT es.total_salary
        FROM employee_salaries es
        JOIN tenant_members tm ON tm.id = es.tenant_member_id
        WHERE tm.tenant_id = $1
          AND es.period_month = (
              SELECT MAX(es2.period_month)
              FROM employee_salaries es2
              WHERE es2.tenant_member_id = es.tenant_member_id
                AND es2.period_month <= $2
          )
        """,
        tenant_id, month_str,
    )

    payroll_base   = Decimal('0')
    transport_base = Decimal('0')

    for r in rows:
        sal = Decimal(str(r['total_salary']))
        payroll_base += sal
        transport_base += sal
        if sal <= _SMMLV_TRANSPORT_THRESHOLD:
            transport_base += _AUXILIO_TRANSPORT_2026

    vacation_base = payroll_base  # transport NOT included for vacaciones

    cesantias  = (transport_base * _PROV_CESANTIAS).quantize(Decimal('1'))
    intereses  = (transport_base * _PROV_INTERESES).quantize(Decimal('1'))
    prima      = (transport_base * _PROV_PRIMA).quantize(Decimal('1'))
    vacaciones = (vacation_base  * _PROV_VACACIONES).quantize(Decimal('1'))

    return {
        'month_str':      month_str,
        'employee_count': len(rows),
        'payroll_base':   payroll_base,
        'transport_base': transport_base,
        'vacation_base':  vacation_base,
        'cesantias':      cesantias,
        'intereses':      intereses,
        'prima':          prima,
        'vacaciones':     vacaciones,
        'total':          cesantias + intereses + prima + vacaciones,
    }


async def preview_provisions(
    request: Request,
    year: int,
    month: int,
) -> ProvisionsPreviewResponse:
    """
    Calculate provisions for a calendar month without writing to the GL.
    Used for P&L preview and close checklist summary.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if not (1 <= month <= 12):
            raise ValidationError("El mes debe estar entre 1 y 12")

        async with get_db_connection() as conn:
            await ensure_colombia_payroll(conn, tenant_id)
            p = await _calculate_provisions(conn, tenant_id, year, month)

        return ProvisionsPreviewResponse(**{
            'period':         p['month_str'],
            'payrollBase':    float(p['payroll_base']),
            'transportBase':  float(p['transport_base']),
            'vacationBase':   float(p['vacation_base']),
            'employeeCount':  p['employee_count'],
            'provisions': ProvisionsBreakdown(**{
                'cesantias':          float(p['cesantias']),
                'interesesCesantias': float(p['intereses']),
                'prima':              float(p['prima']),
                'vacaciones':         float(p['vacaciones']),
                'total':              float(p['total']),
            }),
        })

    except (APIError, AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error previewing provisions: {e}", exc_info=True)
        raise ValidationError("Error al calcular provisiones")


async def post_provisions(
    request: Request,
    year: int,
    month: int,
) -> ProvisionsPostResponse:
    """
    Calculate and post 4 GL entries (one per provision type) for the given month.
    If entries for this period already exist (status=posted), they are voided first.
    Uses source_module='nomina' (within DB CHECK constraint).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("No hay un tenant seleccionado")

        if not (1 <= month <= 12):
            raise ValidationError("El mes debe estar entre 1 y 12")

        month_str = f"{year}-{month:02d}"
        desc_prefix = f"Provisiones nómina {month_str}"

        async with get_db_connection() as conn:
            await ensure_colombia_payroll(conn, tenant_id)
            p = await _calculate_provisions(conn, tenant_id, year, month)
            debit_acct = await resolve_account(
                conn, tenant_id, AccountRole.PAYROLL_EXPENSE, source="accounting_provisions"
            )
            provision_specs = [
                ('cesantias', p['cesantias'], AccountRole.CESANTIAS_PAYABLE, f"{desc_prefix} — Cesantías"),
                ('intereses', p['intereses'], AccountRole.CESANTIAS_INTEREST_PAYABLE, f"{desc_prefix} — Intereses cesantías"),
                ('prima', p['prima'], AccountRole.PRIMA_PAYABLE, f"{desc_prefix} — Prima de servicios"),
                ('vacaciones', p['vacaciones'], AccountRole.VACATION_PAYABLE, f"{desc_prefix} — Vacaciones"),
            ]
            provision_items = []
            for key, amount, role, description in provision_specs:
                if amount == 0:
                    continue
                credit_acct = await resolve_account(
                    conn, tenant_id, role, source="accounting_provisions"
                )
                provision_items.append((key, amount, credit_acct, description))

            entry_ids: List[str] = []
            entry_date = f"{year}-{month:02d}-01"
            async with conn.transaction():
                voided_rows = await conn.fetch(
                    """
                    UPDATE tenant_journal_entries
                    SET status = 'voided', voided_at = NOW()
                    WHERE tenant_id = $1
                      AND source_module = 'nomina'
                      AND description LIKE $2
                      AND status = 'posted'
                    RETURNING id
                    """,
                    tenant_id,
                    f"{desc_prefix}%",
                )
                voided_count = len(voided_rows)

                for _key, amount, credit_acct, description in provision_items:
                    amount_f = float(amount)
                    entry_row = await conn.fetchrow(
                        """
                        INSERT INTO tenant_journal_entries
                        (tenant_id, entry_date, period_year, period_month,
                         description, source_module, status,
                         total_debit, total_credit)
                    VALUES ($1, $2, $3, $4, $5, 'nomina', 'posted', $6, $6)
                    RETURNING id
                    """,
                        tenant_id, entry_date, year, month,
                        description, amount_f,
                    )
                    entry_id = entry_row['id']
                    await conn.execute(
                        """
                        INSERT INTO tenant_journal_lines
                        (journal_entry_id, account_id, debit, credit, description, line_order)
                    VALUES ($1, $2, $3, 0, $4, 1)
                    """,
                        entry_id, debit_acct.id, amount_f, description,
                    )
                    await conn.execute(
                        """
                        INSERT INTO tenant_journal_lines
                        (journal_entry_id, account_id, debit, credit, description, line_order)
                    VALUES ($1, $2, 0, $3, $4, 2)
                    """,
                        entry_id, credit_acct.id, amount_f, description,
                    )
                    entry_ids.append(str(entry_id))

        logger.info(
            f"📋 Provisions posted for tenant {tenant_id}: {month_str} — "
            f"{len(entry_ids)} entries, {voided_count} voided"
        )

        return ProvisionsPostResponse(**{
            'period': month_str,
            'provisions': ProvisionsBreakdown(**{
                'cesantias':          float(p['cesantias']),
                'interesesCesantias': float(p['intereses']),
                'prima':              float(p['prima']),
                'vacaciones':         float(p['vacaciones']),
                'total':              float(p['total']),
            }),
            'journalEntryIds': entry_ids,
            'voidedCount':     voided_count,
        })

    except (APIError, AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Error posting provisions: {e}", exc_info=True)
        raise ValidationError("Error al postear provisiones de nómina")
