import logging
from decimal import Decimal
from datetime import date
from typing import Optional, List
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, AuthorizationError, ValidationError
from app.models.accounting import (
    TenantAccount,
    TenantAccountCreate,
    TenantAccountUpdate,
    TenantAccountResponse,
    TenantAccountsListResponse,
    JournalEntryCreate,
    JournalEntry,
    JournalEntryWithLines,
    JournalLine,
    JournalEntryResponse,
    JournalEntriesListResponse,
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

        level = _derive_level(code)

        async with get_db_connection() as conn:
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

    except (AuthenticationError, AuthorizationError, ValidationError):
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
                entry_row = await conn.fetchrow(
                    """INSERT INTO tenant_journal_entries
                           (tenant_id, entry_date, period_year, period_month,
                            description, reference, source_module, source_id,
                            status, created_by)
                       VALUES ($1, $2, $3, $4, $5, $6, 'manual', NULL, 'draft', $7)
                       RETURNING id, tenant_id, entry_date, period_year, period_month,
                                 description, reference, source_module, source_id,
                                 status, total_debit, total_credit, created_by,
                                 posted_at, voided_at, created_at""",
                    tenant_id, entry_date_obj, period_year, period_month,
                    body.description, body.reference, user_id,
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
        base_query = f"""
            SELECT je.id, je.tenant_id, je.entry_date, je.period_year, je.period_month,
                   je.description, je.reference, je.source_module, je.source_id,
                   je.status, je.total_debit, je.total_credit, je.created_by,
                   je.posted_at, je.voided_at, je.created_at
            FROM tenant_journal_entries je
            WHERE {where_clause}
        """

        async with get_db_connection() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM tenant_journal_entries je WHERE {where_clause}",
                *params,
            )

            offset = (page - 1) * limit
            params.append(limit)
            params.append(offset)
            rows = await conn.fetch(
                base_query + f" ORDER BY je.entry_date DESC, je.created_at DESC "
                             f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        entries = [_row_to_journal_entry(r) for r in rows]
        return JournalEntriesListResponse(data=entries, total=total)

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
                          posted_at, voided_at, created_at
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
                                 posted_at, voided_at, created_at""",
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
                                 posted_at, voided_at, created_at""",
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
                                 posted_at, voided_at, created_at""",
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
