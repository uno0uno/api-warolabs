"""
Salary Service
Handles employee salary configuration and payment management
"""
import logging
import json
from fastapi import Request, UploadFile
from typing import List, Optional, Dict, Any
from uuid import UUID, uuid4
from decimal import Decimal
from datetime import datetime
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import ValidationError, NotFoundError
from app.services.aws_s3_service import AWSS3Service
import asyncpg
from app.models.salary import (
    EmployeesWithSalaryResponse,
    EmployeeDetailResponse,
    SalaryConfigResponse,
    SalaryPaymentResponse,
    SalaryPaymentsListResponse,
    EmployeeSalaryConfigCreate,
    SalaryPaymentCreate,
    EmployeeWithSalary,
    EmployeeDetailWithPayments,
    EmployeeSalaryConfig,
    SalaryPayment,
    SalaryAttachment
)

logger = logging.getLogger(__name__)

# SMMLV 2026 - Should be fetched from database
DEFAULT_SMMLV = Decimal('1423500')

# GL account codes for salary/payroll — debit side (expense)
_SALARY_DEBIT_CODE = {
    "employee":   "5105",  # Sueldos de personal
    "contractor": "5199",  # Otros gastos y honorarios
    "daily":      "5105",  # Sueldos de personal (jornalero)
}

# Slug fallback for credit side (cash/bank) — used when payment_method is not a UUID
# or when payment_methods.gl_account_code is null in DB
_SALARY_CREDIT_SLUG_FALLBACK = {
    "cash":     "1105",  # Caja general
    "transfer": "1110",  # Bancos
    "check":    "1110",  # Bancos
    "other":    "1110",  # Bancos
    "card":     "1110",  # Bancos (datáfono)
    "digital":  "1110",  # Bancos (Nequi, Daviplata, PSE)
    "credit":   "1305",  # Clientes (fiado)
}


def get_color_for_name(name: str) -> str:
    """Generate a consistent color based on name"""
    if not name:
        return '#6B7280'  # Gray for unknown
    colors = [
        '#3B82F6', '#EF4444', '#10B981', '#F59E0B', '#8B5CF6',
        '#EC4899', '#06B6D4', '#84CC16', '#F97316', '#6366F1'
    ]
    return colors[hash(name) % len(colors)]


def get_initials(name: str) -> str:
    """Get initials from name"""
    if not name:
        return '??'
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0][0]}{parts[-1][0]}".upper()
    return name[:2].upper() if len(name) >= 2 else name.upper()


def get_role_label(role: str) -> str:
    """Get human-readable role label"""
    labels = {
        'superuser': 'Super Usuario',
        'admin': 'Administrador',
        'manager': 'Gerente',
        'employee': 'Empleado',
        'cashier': 'Cajero',
        'waiter': 'Mesero',
        'kitchen': 'Cocina'
    }
    return labels.get(role, role.title())


async def get_current_smmlv(conn, tenant_id: UUID) -> Decimal:
    """Get current SMMLV from database or use default"""
    current_period = datetime.now().strftime('%Y-%m')
    result = await conn.fetchval(
        """SELECT minimum_wage_amount FROM minimum_wage_reference
           WHERE tenant_id = $1 AND period_month <= $2
           ORDER BY period_month DESC LIMIT 1""",
        tenant_id, current_period
    )
    return Decimal(str(result)) if result else DEFAULT_SMMLV


async def _resolve_salary_credit_gl_code(conn, payment_method: Optional[str]) -> str:
    """
    Resolve the GL account code for the credit side of a salary payment.
    Mirrors the same cascade used by _post_order_gl_entry in cierre_service.py:
      1. If payment_method is a UUID → query payment_methods JOIN payment_method_groups
         for COALESCE(pm.gl_account_code, pmg.gl_account_code)
      2. Fall back to slug-based dict (_SALARY_CREDIT_SLUG_FALLBACK)
      3. Ultimate fallback: "1110" (Bancos)
    """
    if payment_method:
        try:
            UUID(payment_method)
            is_uuid = True
        except (ValueError, AttributeError):
            is_uuid = False

        if is_uuid:
            row = await conn.fetchrow(
                """SELECT COALESCE(pm.gl_account_code, pmg.gl_account_code) AS code
                   FROM payment_methods pm
                   JOIN payment_method_groups pmg ON pm.group_id = pmg.id
                   WHERE pm.id = $1""",
                UUID(payment_method),
            )
            if row and row["code"]:
                return row["code"]

        slug_code = _SALARY_CREDIT_SLUG_FALLBACK.get(payment_method)
        if slug_code:
            return slug_code

    return "1110"


async def _post_salary_gl_entry(
    conn,
    tenant_id: UUID,
    payment_id: UUID,
    payment_date,
    payment_amount: Decimal,
    employment_type: Optional[str],
    payment_method: Optional[str],
    description: str,
) -> None:
    """
    Post a double-entry GL journal entry for a salary payment.
    DR: 5105 (employee/daily) or 5199 (contractor)
    CR: 1105 (cash) or 1110 (transfer/other)
    Silently skips if accounts missing, period closed, or already posted.
    Caller MUST wrap in try/except.
    """
    if payment_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina' AND source_id = $2 AND status = 'posted'""",
        tenant_id, payment_id
    )
    if already_posted:
        logger.info(f"[GL] Salary payment {payment_id} already posted — skip")
        return

    # Check period open
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else payment_date
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(f"[GL] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for salary payment {payment_id}")
        return

    # Resolve debit account (expense)
    debit_code = _SALARY_DEBIT_CODE.get(employment_type or "employee", "5105")
    debit_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, debit_code,
    )
    if not debit_acct:
        logger.warning(f"[GL] Debit account {debit_code} not found for tenant {tenant_id} — skip salary GL")
        return

    # Resolve credit account (cash/bank) — dynamic UUID-aware lookup
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    credit_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not credit_acct:
        logger.warning(f"[GL] Credit account {credit_code} not found for tenant {tenant_id} — skip salary GL")
        return

    amt = float(payment_amount)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            description, payment_id, amt, amt,
        )
        entry_id = entry_row["id"]

        # Debit line — expense account
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, debit_acct["id"], amt, description,
        )

        # Credit line — cash/bank account
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, credit_acct["id"], amt, description,
        )

    logger.info(f"[GL] Posted salary entry {entry_id} for payment {payment_id} (amount={amt}, debit={debit_code}, credit={credit_code})")


async def get_employees_with_salary(request: Request) -> EmployeesWithSalaryResponse:
    """Get all employees with their salary configuration"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        smmlv = await get_current_smmlv(conn, tenant_id)
        current_period = datetime.now().strftime('%Y-%m')

        # Get employees with salary config and last payment
        query = """
            SELECT
                tm.id,
                tm.user_id,
                p.name,
                p.email,
                tm.role,
                es.salary_type,
                es.minimum_wage_multiplier as multiplier,
                es.fixed_amount,
                es.hourly_rate,
                es.payment_frequency,
                es.notes as salary_notes,
                es.employment_type,
                es.daily_rate,
                (SELECT payment_date FROM salary_payments sp
                 WHERE sp.tenant_member_id = tm.id
                 ORDER BY payment_date DESC LIMIT 1) as last_payment_date,
                (SELECT payment_amount FROM salary_payments sp
                 WHERE sp.tenant_member_id = tm.id
                 ORDER BY payment_date DESC LIMIT 1) as last_payment_amount,
                (SELECT period_month FROM salary_payments sp
                 WHERE sp.tenant_member_id = tm.id
                 ORDER BY payment_date DESC LIMIT 1) as last_payment_period
            FROM tenant_members tm
            JOIN profile p ON p.id = tm.user_id
            LEFT JOIN employee_salaries es ON es.tenant_member_id = tm.id
                AND es.period_month = $2
            WHERE tm.tenant_id = $1
                AND tm.role != 'customer'
            ORDER BY p.name
        """

        rows = await conn.fetch(query, tenant_id, current_period)

        employees = []
        for row in rows:
            # Calculate salary based on type
            calculated_salary = Decimal('0')
            if row['salary_type'] == 'smmlv' and row['multiplier']:
                calculated_salary = Decimal(str(row['multiplier'])) * smmlv
            elif row['salary_type'] == 'fixed' and row['fixed_amount']:
                calculated_salary = Decimal(str(row['fixed_amount']))
            elif row['salary_type'] == 'hourly' and row['hourly_rate']:
                calculated_salary = Decimal(str(row['hourly_rate']))  # For hourly, display the rate

            name = row['name'] or row['email'] or 'Sin nombre'
            employees.append(EmployeeWithSalary(
                id=row['id'],
                user_id=row['user_id'],
                name=name,
                email=row['email'],
                role=row['role'],
                role_label=get_role_label(row['role']),
                initials=get_initials(name),
                color=get_color_for_name(name),
                salary_type=row['salary_type'],
                multiplier=Decimal(str(row['multiplier'])) if row['multiplier'] else None,
                fixed_amount=Decimal(str(row['fixed_amount'])) if row['fixed_amount'] else None,
                hourly_rate=Decimal(str(row['hourly_rate'])) if row.get('hourly_rate') else None,
                calculated_salary=calculated_salary,
                salary_notes=row['salary_notes'],
                employment_type=row['employment_type'],
                daily_rate=Decimal(str(row['daily_rate'])) if row.get('daily_rate') else None,
                last_payment_date=row['last_payment_date'],
                last_payment_amount=Decimal(str(row['last_payment_amount'])) if row['last_payment_amount'] else None,
                last_payment_period=row['last_payment_period']
            ))

        return EmployeesWithSalaryResponse(
            success=True,
            data=employees,
            smmlv=smmlv
        )


async def get_employee_salary_detail(request: Request, employee_id: UUID) -> EmployeeDetailResponse:
    """Get employee detail with salary config and payment history"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        smmlv = await get_current_smmlv(conn, tenant_id)
        current_period = datetime.now().strftime('%Y-%m')
        current_year = datetime.now().year

        # Get employee with salary config
        employee_query = """
            SELECT
                tm.id,
                tm.user_id,
                p.name,
                p.email,
                tm.role,
                es.salary_type,
                es.minimum_wage_multiplier as multiplier,
                es.fixed_amount,
                es.hourly_rate,
                es.notes as salary_notes,
                es.employment_type,
                es.daily_rate
            FROM tenant_members tm
            JOIN profile p ON p.id = tm.user_id
            LEFT JOIN employee_salaries es ON es.tenant_member_id = tm.id
                AND es.period_month = $2
            WHERE tm.id = $1 AND tm.tenant_id = $3
        """

        row = await conn.fetchrow(employee_query, employee_id, current_period, tenant_id)

        if not row:
            raise NotFoundError("Employee not found")

        # Calculate salary
        calculated_salary = Decimal('0')
        if row['salary_type'] == 'smmlv' and row['multiplier']:
            calculated_salary = Decimal(str(row['multiplier'])) * smmlv
        elif row['salary_type'] == 'fixed' and row['fixed_amount']:
            calculated_salary = Decimal(str(row['fixed_amount']))
        elif row['salary_type'] == 'hourly' and row['hourly_rate']:
            calculated_salary = Decimal(str(row['hourly_rate']))  # For hourly, display the rate

        # Get payments with attachments
        payments_query = """
            SELECT
                sp.id,
                sp.tenant_id,
                sp.tenant_member_id,
                sp.period_month,
                sp.payment_amount,
                sp.payment_method,
                sp.payment_reference,
                sp.payment_date,
                sp.notes,
                sp.status,
                sp.days_worked,
                sp.created_by,
                sp.created_at,
                sp.updated_at
            FROM salary_payments sp
            WHERE sp.tenant_member_id = $1 AND sp.tenant_id = $2
            ORDER BY sp.payment_date DESC
            LIMIT 50
        """

        payment_rows = await conn.fetch(payments_query, employee_id, tenant_id)

        payments = []
        for prow in payment_rows:
            # Get attachments for this payment
            attachments_query = """
                SELECT id, tenant_id, salary_payment_id, path, file_name,
                       file_size, mime_type, s3_key, uploaded_by, uploaded_at
                FROM salary_attachments
                WHERE salary_payment_id = $1
            """
            attachment_rows = await conn.fetch(attachments_query, prow['id'])

            attachments = [
                SalaryAttachment(
                    id=a['id'],
                    tenant_id=a['tenant_id'],
                    salary_payment_id=a['salary_payment_id'],
                    path=a['path'],
                    file_name=a['file_name'],
                    file_size=a['file_size'],
                    mime_type=a['mime_type'],
                    s3_key=a['s3_key'],
                    uploaded_by=a['uploaded_by'],
                    uploaded_at=a['uploaded_at']
                )
                for a in attachment_rows
            ]

            payments.append(SalaryPayment(
                id=prow['id'],
                tenant_id=prow['tenant_id'],
                tenant_member_id=prow['tenant_member_id'],
                period_month=prow['period_month'],
                payment_amount=Decimal(str(prow['payment_amount'])),
                payment_method=prow['payment_method'],
                payment_reference=prow['payment_reference'],
                payment_date=prow['payment_date'],
                notes=prow['notes'],
                status=prow.get('status', 'paid'),
                days_worked=prow.get('days_worked'),
                created_by=prow['created_by'],
                created_at=prow['created_at'],
                updated_at=prow['updated_at'],
                attachments=attachments
            ))

        # Calculate totals
        total_paid_query = """
            SELECT COALESCE(SUM(payment_amount), 0) as total
            FROM salary_payments
            WHERE tenant_member_id = $1
              AND tenant_id = $2
              AND EXTRACT(YEAR FROM payment_date) = $3
        """
        total_paid = await conn.fetchval(total_paid_query, employee_id, tenant_id, current_year)

        name = row['name'] or row['email'] or 'Sin nombre'
        employee = EmployeeDetailWithPayments(
            id=row['id'],
            user_id=row['user_id'],
            name=name,
            email=row['email'],
            role=row['role'],
            role_label=get_role_label(row['role']),
            initials=get_initials(name),
            color=get_color_for_name(name),
            salary_type=row['salary_type'],
            multiplier=Decimal(str(row['multiplier'])) if row['multiplier'] else None,
            fixed_amount=Decimal(str(row['fixed_amount'])) if row['fixed_amount'] else None,
            hourly_rate=Decimal(str(row['hourly_rate'])) if row.get('hourly_rate') else None,
            calculated_salary=calculated_salary,
            salary_notes=row['salary_notes'],
            employment_type=row['employment_type'],
            daily_rate=Decimal(str(row['daily_rate'])) if row.get('daily_rate') else None,
            payments=payments,
            total_paid_this_year=Decimal(str(total_paid)) if total_paid else Decimal('0'),
            payments_count=len(payments)
        )

        return EmployeeDetailResponse(
            success=True,
            data=employee,
            smmlv=smmlv
        )


async def configure_employee_salary(
    request: Request,
    employee_id: UUID,
    config: EmployeeSalaryConfigCreate
) -> SalaryConfigResponse:
    """Configure or update employee salary"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id

    async with get_db_connection() as conn:
        smmlv = await get_current_smmlv(conn, tenant_id)
        current_period = datetime.now().strftime('%Y-%m')

        # Verify employee belongs to tenant
        employee = await conn.fetchrow(
            "SELECT id FROM tenant_members WHERE id = $1 AND tenant_id = $2",
            employee_id, tenant_id
        )
        if not employee:
            raise NotFoundError("Employee not found")

        # Validate employment type constraints
        if config.employment_type == 'daily' and config.daily_rate is None:
            raise ValidationError("daily_rate is required for daily employment type")

        # Calculate base and total salary
        if config.salary_type == 'smmlv':
            if config.minimum_wage_multiplier is None:
                raise ValidationError("minimum_wage_multiplier is required for SMMLV salary type")
            base_salary = config.minimum_wage_multiplier * smmlv
            total_salary = base_salary
        elif config.salary_type == 'hourly':
            if config.hourly_rate is None:
                raise ValidationError("hourly_rate is required for hourly salary type")
            base_salary = config.hourly_rate
            total_salary = base_salary
        else:  # fixed
            if config.fixed_amount is None:
                raise ValidationError("fixed_amount is required for fixed salary type")
            base_salary = config.fixed_amount
            total_salary = base_salary

        # Upsert salary config
        upsert_query = """
            INSERT INTO employee_salaries (
                id, tenant_member_id, period_month, salary_type,
                minimum_wage_multiplier, fixed_amount, hourly_rate, base_salary,
                total_salary, notes, employment_type, daily_rate, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
            ON CONFLICT (tenant_member_id, period_month)
            DO UPDATE SET
                salary_type = EXCLUDED.salary_type,
                minimum_wage_multiplier = EXCLUDED.minimum_wage_multiplier,
                fixed_amount = EXCLUDED.fixed_amount,
                hourly_rate = EXCLUDED.hourly_rate,
                base_salary = EXCLUDED.base_salary,
                total_salary = EXCLUDED.total_salary,
                notes = EXCLUDED.notes,
                employment_type = EXCLUDED.employment_type,
                daily_rate = EXCLUDED.daily_rate
            RETURNING id, tenant_member_id, period_month, salary_type,
                      minimum_wage_multiplier, fixed_amount, hourly_rate, base_salary,
                      total_salary, notes, employment_type, daily_rate, created_at
        """

        row = await conn.fetchrow(
            upsert_query,
            uuid4(),
            employee_id,
            current_period,
            config.salary_type,
            config.minimum_wage_multiplier,
            config.fixed_amount,
            config.hourly_rate,
            base_salary,
            total_salary,
            config.notes,
            config.employment_type,
            config.daily_rate
        )

        salary_config = EmployeeSalaryConfig(
            id=row['id'],
            tenant_member_id=row['tenant_member_id'],
            period_month=row['period_month'],
            salary_type=row['salary_type'],
            minimum_wage_multiplier=Decimal(str(row['minimum_wage_multiplier'])) if row['minimum_wage_multiplier'] else None,
            fixed_amount=Decimal(str(row['fixed_amount'])) if row['fixed_amount'] else None,
            hourly_rate=Decimal(str(row['hourly_rate'])) if row.get('hourly_rate') else None,
            base_salary=Decimal(str(row['base_salary'])),
            total_salary=Decimal(str(row['total_salary'])),
            calculated_salary=total_salary,
            notes=row['notes'],
            employment_type=row['employment_type'],
            daily_rate=Decimal(str(row['daily_rate'])) if row.get('daily_rate') else None,
            created_at=row['created_at']
        )

        logger.info(f"Salary configured for employee {employee_id}: {config.salary_type}")

        return SalaryConfigResponse(success=True, data=salary_config)


async def record_salary_payment_json(
    request: Request,
    payment_data: 'SalaryPaymentCreate'
) -> SalaryPaymentResponse:
    """Record a salary payment from JSON payload (no file attachments)"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id

    async with get_db_connection() as conn:
        # Verify employee belongs to tenant
        employee = await conn.fetchrow(
            "SELECT id FROM tenant_members WHERE id = $1 AND tenant_id = $2",
            payment_data.tenant_member_id, tenant_id
        )
        if not employee:
            raise NotFoundError("Employee not found")

        # Fetch salary config to get employment_type and daily_rate
        salary_config = await conn.fetchrow(
            """SELECT employment_type, daily_rate FROM employee_salaries
               WHERE tenant_member_id = $1 ORDER BY period_month DESC LIMIT 1""",
            payment_data.tenant_member_id
        )
        employment_type = salary_config['employment_type'] if salary_config else 'employee'
        daily_rate = Decimal(str(salary_config['daily_rate'])) if salary_config and salary_config['daily_rate'] else None

        # Auto-calculate amount for daily workers
        payment_amount = payment_data.payment_amount
        if employment_type == 'daily' and payment_data.days_worked and daily_rate:
            payment_amount = Decimal(str(payment_data.days_worked)) * daily_rate

        # Create payment
        payment_id = uuid4()
        insert_query = """
            INSERT INTO salary_payments (
                id, tenant_id, tenant_member_id, period_month,
                payment_amount, payment_method, payment_reference,
                payment_date, notes, status, days_worked, created_by, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW(), NOW())
            RETURNING id, tenant_id, tenant_member_id, period_month,
                      payment_amount, payment_method, payment_reference,
                      payment_date, notes, status, days_worked, created_by, created_at, updated_at
        """

        row = await conn.fetchrow(
            insert_query,
            payment_id,
            tenant_id,
            payment_data.tenant_member_id,
            payment_data.period_month,
            payment_amount,
            payment_data.payment_method,
            payment_data.payment_reference,
            payment_data.payment_date,
            payment_data.notes,
            payment_data.status if hasattr(payment_data, 'status') else 'paid',
            payment_data.days_worked,
            user_id
        )

        # Post GL entry (graceful degrade — never blocks payment)
        try:
            employee_name_row = await conn.fetchrow(
                """SELECT p.name FROM tenant_members tm JOIN profile p ON p.id = tm.user_id WHERE tm.id = $1""",
                payment_data.tenant_member_id
            )
            emp_name = employee_name_row['name'] if employee_name_row else 'Empleado'
            gl_description = f"Salario {payment_data.period_month} — {emp_name}"
            await _post_salary_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                payment_id=payment_id,
                payment_date=payment_data.payment_date,
                payment_amount=payment_amount,
                employment_type=employment_type,
                payment_method=str(payment_data.payment_method) if payment_data.payment_method else None,
                description=gl_description,
            )
        except Exception as gl_err:
            logger.warning(f"[GL] Salary GL posting failed for payment {payment_id}: {gl_err}")

        payment = SalaryPayment(
            id=row['id'],
            tenant_id=row['tenant_id'],
            tenant_member_id=row['tenant_member_id'],
            period_month=row['period_month'],
            payment_amount=Decimal(str(row['payment_amount'])),
            payment_method=row['payment_method'],
            payment_reference=row['payment_reference'],
            payment_date=row['payment_date'],
            notes=row['notes'],
            status=row.get('status', 'paid'),
            days_worked=row.get('days_worked'),
            created_by=row['created_by'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            attachments=[]
        )

        logger.info(f"Salary payment recorded for employee {payment_data.tenant_member_id}: {payment_amount}")

        return SalaryPaymentResponse(success=True, data=payment)


async def record_salary_payment(
    request: Request,
    tenant_member_id: UUID,
    payment_amount: Decimal,
    payment_method: str,
    payment_date: datetime,
    period_month: str,
    payment_reference: Optional[str] = None,
    notes: Optional[str] = None,
    days_worked: Optional[int] = None,
    attachments: List[UploadFile] = []
) -> SalaryPaymentResponse:
    """Record a salary payment with optional attachments"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id

    async with get_db_connection() as conn:
        # Verify employee belongs to tenant
        employee = await conn.fetchrow(
            "SELECT id FROM tenant_members WHERE id = $1 AND tenant_id = $2",
            tenant_member_id, tenant_id
        )
        if not employee:
            raise NotFoundError("Employee not found")

        # Fetch salary config to get employment_type and daily_rate
        salary_config_row = await conn.fetchrow(
            """SELECT employment_type, daily_rate FROM employee_salaries
               WHERE tenant_member_id = $1 ORDER BY period_month DESC LIMIT 1""",
            tenant_member_id
        )
        mp_employment_type = salary_config_row['employment_type'] if salary_config_row else 'employee'
        mp_daily_rate = Decimal(str(salary_config_row['daily_rate'])) if salary_config_row and salary_config_row['daily_rate'] else None

        # Auto-calculate amount for daily workers
        actual_payment_amount = payment_amount
        if mp_employment_type == 'daily' and days_worked and mp_daily_rate:
            actual_payment_amount = Decimal(str(days_worked)) * mp_daily_rate

        # Create payment
        payment_id = uuid4()
        insert_query = """
            INSERT INTO salary_payments (
                id, tenant_id, tenant_member_id, period_month,
                payment_amount, payment_method, payment_reference,
                payment_date, notes, days_worked, created_by, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())
            RETURNING id, tenant_id, tenant_member_id, period_month,
                      payment_amount, payment_method, payment_reference,
                      payment_date, notes, days_worked, created_by, created_at, updated_at
        """

        row = await conn.fetchrow(
            insert_query,
            payment_id,
            tenant_id,
            tenant_member_id,
            period_month,
            actual_payment_amount,
            payment_method,
            payment_reference,
            payment_date,
            notes,
            days_worked,
            user_id
        )

        # Handle attachments
        uploaded_attachments = []
        if attachments:
            from io import BytesIO
            s3_service = AWSS3Service()
            for file in attachments:
                if file.filename:
                    try:
                        content = await file.read()
                        file_obj = BytesIO(content)
                        s3_key = await s3_service.upload_file(
                            file_content=file_obj,
                            filename=file.filename,
                            folder=f"salaries/{tenant_id}",
                            content_type=file.content_type
                        )

                        if s3_key:
                            # Get presigned URL
                            path = await s3_service.get_presigned_url(s3_key) or s3_key

                            # Insert attachment record
                            att_query = """
                                INSERT INTO salary_attachments (
                                    id, tenant_id, salary_payment_id, path,
                                    file_name, file_size, mime_type, s3_key,
                                    uploaded_by, uploaded_at
                                )
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                                RETURNING id, tenant_id, salary_payment_id, path,
                                          file_name, file_size, mime_type, s3_key,
                                          uploaded_by, uploaded_at
                            """
                            att_row = await conn.fetchrow(
                                att_query,
                                uuid4(),
                                tenant_id,
                                payment_id,
                                path,
                                file.filename,
                                len(content),
                                file.content_type,
                                s3_key,
                                user_id
                            )

                            uploaded_attachments.append(SalaryAttachment(
                                id=att_row['id'],
                                tenant_id=att_row['tenant_id'],
                                salary_payment_id=att_row['salary_payment_id'],
                                path=att_row['path'],
                                file_name=att_row['file_name'],
                                file_size=att_row['file_size'],
                                mime_type=att_row['mime_type'],
                                s3_key=att_row['s3_key'],
                                uploaded_by=att_row['uploaded_by'],
                                uploaded_at=att_row['uploaded_at']
                            ))
                    except Exception as e:
                        logger.error(f"Error uploading attachment: {e}")

        # Post GL entry (graceful degrade — never blocks payment)
        try:
            mp_emp_name_row = await conn.fetchrow(
                """SELECT p.name FROM tenant_members tm JOIN profile p ON p.id = tm.user_id WHERE tm.id = $1""",
                tenant_member_id
            )
            mp_emp_name = mp_emp_name_row['name'] if mp_emp_name_row else 'Empleado'
            mp_gl_description = f"Salario {period_month} — {mp_emp_name}"
            await _post_salary_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                payment_id=payment_id,
                payment_date=payment_date,
                payment_amount=actual_payment_amount,
                employment_type=mp_employment_type,
                payment_method=str(payment_method) if payment_method else None,
                description=mp_gl_description,
            )
        except Exception as gl_err:
            logger.warning(f"[GL] Salary GL posting failed for payment {payment_id}: {gl_err}")

        payment = SalaryPayment(
            id=row['id'],
            tenant_id=row['tenant_id'],
            tenant_member_id=row['tenant_member_id'],
            period_month=row['period_month'],
            payment_amount=Decimal(str(row['payment_amount'])),
            payment_method=row['payment_method'],
            payment_reference=row['payment_reference'],
            payment_date=row['payment_date'],
            notes=row['notes'],
            days_worked=row.get('days_worked'),
            created_by=row['created_by'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            attachments=uploaded_attachments
        )

        logger.info(f"Salary payment recorded for employee {tenant_member_id}: {actual_payment_amount}")

        return SalaryPaymentResponse(success=True, data=payment)


async def get_salary_payments(
    request: Request,
    employee_id: Optional[UUID] = None,
    period_month: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> SalaryPaymentsListResponse:
    """Get salary payments with optional filters"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Build query with filters
        where_clauses = ["sp.tenant_id = $1"]
        params = [tenant_id]
        param_count = 1

        if employee_id:
            param_count += 1
            where_clauses.append(f"sp.tenant_member_id = ${param_count}")
            params.append(employee_id)

        if period_month:
            param_count += 1
            where_clauses.append(f"sp.period_month = ${param_count}")
            params.append(period_month)

        where_clause = " AND ".join(where_clauses)

        # Count total
        count_query = f"SELECT COUNT(*) FROM salary_payments sp WHERE {where_clause}"
        total = await conn.fetchval(count_query, *params)

        # Get payments
        query = f"""
            SELECT
                sp.id, sp.tenant_id, sp.tenant_member_id, sp.period_month,
                sp.payment_amount, sp.payment_method, sp.payment_reference,
                sp.payment_date, sp.notes, sp.created_by, sp.created_at, sp.updated_at
            FROM salary_payments sp
            WHERE {where_clause}
            ORDER BY sp.payment_date DESC
            LIMIT ${param_count + 1} OFFSET ${param_count + 2}
        """
        params.extend([limit, offset])

        rows = await conn.fetch(query, *params)

        payments = []
        for row in rows:
            # Get attachments
            att_rows = await conn.fetch(
                "SELECT * FROM salary_attachments WHERE salary_payment_id = $1",
                row['id']
            )
            attachments = [
                SalaryAttachment(
                    id=a['id'],
                    tenant_id=a['tenant_id'],
                    salary_payment_id=a['salary_payment_id'],
                    path=a['path'],
                    file_name=a['file_name'],
                    file_size=a['file_size'],
                    mime_type=a['mime_type'],
                    s3_key=a['s3_key'],
                    uploaded_by=a['uploaded_by'],
                    uploaded_at=a['uploaded_at']
                )
                for a in att_rows
            ]

            payments.append(SalaryPayment(
                id=row['id'],
                tenant_id=row['tenant_id'],
                tenant_member_id=row['tenant_member_id'],
                period_month=row['period_month'],
                payment_amount=Decimal(str(row['payment_amount'])),
                payment_method=row['payment_method'],
                payment_reference=row['payment_reference'],
                payment_date=row['payment_date'],
                notes=row['notes'],
                created_by=row['created_by'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                attachments=attachments
            ))

        return SalaryPaymentsListResponse(
            success=True,
            data=payments,
            total=total
        )


async def get_payment_detail(request: Request, payment_id: UUID) -> Dict[str, Any]:
    """Get payment detail with attachments and employee info"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Get payment with employee info
        row = await conn.fetchrow(
            """SELECT
                sp.*,
                p.name as employee_name,
                tm.role as employee_role
               FROM salary_payments sp
               LEFT JOIN tenant_members tm ON sp.tenant_member_id = tm.id
               LEFT JOIN profile p ON tm.user_id = p.id
               WHERE sp.id = $1 AND sp.tenant_id = $2""",
            payment_id, tenant_id
        )

        if not row:
            raise NotFoundError("Payment not found")

        # Get attachments
        att_rows = await conn.fetch(
            "SELECT * FROM salary_attachments WHERE salary_payment_id = $1",
            payment_id
        )
        attachments = [
            {
                'id': str(a['id']),
                'tenant_id': str(a['tenant_id']),
                'salary_payment_id': str(a['salary_payment_id']),
                'path': a['path'],
                'file_name': a['file_name'],
                'file_size': a['file_size'],
                'mime_type': a['mime_type'],
                's3_key': a['s3_key'],
                'uploaded_by': str(a['uploaded_by']) if a['uploaded_by'] else None,
                'uploaded_at': a['uploaded_at'].isoformat() if a['uploaded_at'] else None
            }
            for a in att_rows
        ]

        payment_data = {
            'id': str(row['id']),
            'tenant_id': str(row['tenant_id']),
            'tenant_member_id': str(row['tenant_member_id']),
            'period_month': row['period_month'],
            'payment_amount': float(row['payment_amount']),
            'payment_method': row['payment_method'],
            'payment_reference': row['payment_reference'],
            'payment_date': row['payment_date'].isoformat() if row['payment_date'] else None,
            'notes': row['notes'],
            'status': row.get('status', 'paid'),
            'days_worked': row.get('days_worked'),
            'created_by': str(row['created_by']) if row['created_by'] else None,
            'created_at': row['created_at'].isoformat() if row['created_at'] else None,
            'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
            'attachments': attachments
        }

        employee_data = {
            'id': str(row['tenant_member_id']),
            'name': row['employee_name'],
            'role': row['employee_role'],
            'role_label': get_role_label(row['employee_role'])
        }

        return {
            'success': True,
            'data': payment_data,
            'employee': employee_data
        }


async def delete_salary_payment(request: Request, payment_id: UUID) -> dict:
    """Delete a salary payment"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify payment exists and belongs to tenant
        payment = await conn.fetchrow(
            "SELECT id FROM salary_payments WHERE id = $1 AND tenant_id = $2",
            payment_id, tenant_id
        )

        if not payment:
            raise NotFoundError("Payment not found")

        # Fetch and delete all S3 files before deleting from database
        att_rows = await conn.fetch(
            "SELECT s3_key FROM salary_attachments WHERE salary_payment_id = $1",
            payment_id
        )
        s3_service = AWSS3Service()
        for att in att_rows:
            if att['s3_key']:
                try:
                    await s3_service.delete_file(att['s3_key'])
                except Exception as e:
                    logger.error(f"Error deleting S3 file during payment deletion: {e}")

        # Delete attachments first (cascade should handle this, but be explicit)
        await conn.execute(
            "DELETE FROM salary_attachments WHERE salary_payment_id = $1",
            payment_id
        )

        # Delete payment
        await conn.execute(
            "DELETE FROM salary_payments WHERE id = $1",
            payment_id
        )

        logger.info(f"Salary payment {payment_id} deleted with {len(att_rows)} S3 files")

        return {"success": True, "message": "Payment deleted successfully"}


async def _track_salary_payment_change(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    payment_id: UUID,
    change_type: str,
    user_id: Optional[UUID] = None,
    field_changed: Optional[str] = None,
    old_value: Optional[Any] = None,
    new_value: Optional[Any] = None,
    payment_snapshot: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None
):
    """
    Helper function to insert a record into salary_payment_change_history
    """
    await conn.execute("""
        INSERT INTO salary_payment_change_history (
            tenant_id,
            payment_id,
            change_type,
            field_changed,
            old_value,
            new_value,
            payment_snapshot,
            changed_by,
            notes
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    """,
        tenant_id,
        payment_id,
        change_type,
        field_changed,
        json.dumps(old_value) if old_value is not None else None,
        json.dumps(new_value) if new_value is not None else None,
        json.dumps(payment_snapshot) if payment_snapshot else None,
        user_id,
        notes
    )


async def update_salary_payment(
    request: Request,
    payment_id: UUID,
    payment_amount: Optional[Decimal] = None,
    payment_date: Optional[datetime] = None,
    payment_method: Optional[str] = None,
    payment_reference: Optional[str] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None
) -> SalaryPaymentResponse:
    """
    Update an existing salary payment
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id

    async with get_db_connection() as conn:
        # Fetch old payment data for comparison
        old_payment = await conn.fetchrow("""
            SELECT
                payment_amount,
                payment_date,
                payment_method,
                payment_reference,
                notes,
                status
            FROM salary_payments
            WHERE id = $1 AND tenant_id = $2
        """, payment_id, tenant_id)

        if not old_payment:
            raise NotFoundError("Payment not found")

        # Build dynamic UPDATE query
        update_fields = []
        params = []
        param_count = 1
        changes_tracked = []

        # Track and update payment_amount
        if payment_amount is not None and Decimal(str(old_payment['payment_amount'])) != payment_amount:
            update_fields.append(f"payment_amount = ${param_count}")
            params.append(payment_amount)
            param_count += 1

            await _track_salary_payment_change(
                conn, tenant_id, payment_id, 'field_update', user_id,
                field_changed='payment_amount',
                old_value={'payment_amount': float(old_payment['payment_amount'])},
                new_value={'payment_amount': float(payment_amount)}
            )
            changes_tracked.append('payment_amount')

        # Track and update payment_date
        if payment_date is not None and old_payment['payment_date'] != payment_date:
            update_fields.append(f"payment_date = ${param_count}")
            params.append(payment_date)
            param_count += 1

            await _track_salary_payment_change(
                conn, tenant_id, payment_id, 'field_update', user_id,
                field_changed='payment_date',
                old_value={'payment_date': old_payment['payment_date'].isoformat() if old_payment['payment_date'] else None},
                new_value={'payment_date': payment_date.isoformat()}
            )
            changes_tracked.append('payment_date')

        # Track and update payment_method
        if payment_method is not None and old_payment['payment_method'] != payment_method:
            update_fields.append(f"payment_method = ${param_count}")
            params.append(payment_method)
            param_count += 1

            await _track_salary_payment_change(
                conn, tenant_id, payment_id, 'field_update', user_id,
                field_changed='payment_method',
                old_value={'payment_method': old_payment['payment_method']},
                new_value={'payment_method': payment_method}
            )
            changes_tracked.append('payment_method')

        # Track and update payment_reference
        if payment_reference is not None and old_payment['payment_reference'] != payment_reference:
            update_fields.append(f"payment_reference = ${param_count}")
            params.append(payment_reference)
            param_count += 1

            await _track_salary_payment_change(
                conn, tenant_id, payment_id, 'field_update', user_id,
                field_changed='payment_reference',
                old_value={'payment_reference': old_payment['payment_reference']},
                new_value={'payment_reference': payment_reference}
            )
            changes_tracked.append('payment_reference')

        # Track and update notes
        if notes is not None and old_payment['notes'] != notes:
            update_fields.append(f"notes = ${param_count}")
            params.append(notes)
            param_count += 1

            await _track_salary_payment_change(
                conn, tenant_id, payment_id, 'field_update', user_id,
                field_changed='notes',
                old_value={'notes': old_payment['notes']},
                new_value={'notes': notes}
            )
            changes_tracked.append('notes')

        # Track and update status
        if status is not None and old_payment['status'] != status:
            update_fields.append(f"status = ${param_count}")
            params.append(status)
            param_count += 1

            await _track_salary_payment_change(
                conn, tenant_id, payment_id, 'status_change', user_id,
                field_changed='status',
                old_value={'status': old_payment['status']},
                new_value={'status': status}
            )
            changes_tracked.append('status')

        if not update_fields:
            raise ValidationError("No fields to update")

        # Add updated_at
        update_fields.append("updated_at = NOW()")

        # Execute UPDATE
        params.extend([payment_id, tenant_id])
        query = f"""
            UPDATE salary_payments
            SET {', '.join(update_fields)}
            WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
        """

        await conn.execute(query, *params)

        logger.info(f"Updated salary payment {payment_id}, tracked {len(changes_tracked)} changes: {changes_tracked}")

        # Fetch and return updated payment
        return await get_payment_detail(request, payment_id)


async def get_salary_payment_history(
    request: Request,
    payment_id: UUID
) -> List[Dict[str, Any]]:
    """
    Get change history for a specific salary payment
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify payment belongs to tenant
        payment = await conn.fetchrow("""
            SELECT id FROM salary_payments WHERE id = $1 AND tenant_id = $2
        """, payment_id, tenant_id)

        if not payment:
            raise NotFoundError("Payment not found")

        # Fetch history with user info
        history_rows = await conn.fetch("""
            SELECT
                h.id,
                h.tenant_id,
                h.payment_id,
                h.change_type,
                h.field_changed,
                h.old_value,
                h.new_value,
                h.payment_snapshot,
                h.changed_by,
                h.changed_at,
                h.notes,
                h.created_at,
                p.email as changed_by_email,
                p.name as changed_by_name
            FROM salary_payment_change_history h
            LEFT JOIN profile p ON h.changed_by = p.id
            WHERE h.payment_id = $1 AND h.tenant_id = $2
            ORDER BY h.changed_at DESC
        """, payment_id, tenant_id)

        history = []
        for row in history_rows:
            history_item = {
                'id': str(row['id']),
                'tenantId': str(row['tenant_id']),
                'paymentId': str(row['payment_id']),
                'changeType': row['change_type'],
                'fieldChanged': row['field_changed'],
                'oldValue': row['old_value'],
                'newValue': row['new_value'],
                'paymentSnapshot': row['payment_snapshot'],
                'changedBy': str(row['changed_by']) if row['changed_by'] else None,
                'changedAt': row['changed_at'].isoformat() if row['changed_at'] else None,
                'notes': row['notes'],
                'createdAt': row['created_at'].isoformat() if row['created_at'] else None,
                'changedByEmail': row['changed_by_email'],
                'changedByName': row['changed_by_name']
            }
            history.append(history_item)

        return history


async def upload_salary_payment_attachments(
    request: Request,
    payment_id: UUID,
    files: List[UploadFile]
) -> Dict[str, Any]:
    """
    Upload attachments for existing salary payment
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id

    async with get_db_connection() as conn:
        # Verify payment exists
        payment = await conn.fetchrow(
            "SELECT id FROM salary_payments WHERE id = $1 AND tenant_id = $2",
            payment_id, tenant_id
        )

        if not payment:
            raise NotFoundError("Payment not found")

        s3_service = AWSS3Service()
        uploaded_files = []

        for file in files:
            try:
                # Upload to S3
                s3_key = f"salaries/{tenant_id}/{uuid4()}_{file.filename}"
                uploaded_key = await s3_service.upload_file_with_key(
                    file_content=await file.read(),
                    s3_key=s3_key,
                    content_type=file.content_type
                )

                if not uploaded_key:
                    logger.error(f"Failed to upload file {file.filename} to S3")
                    continue

                # Generate presigned URL for the uploaded file
                s3_url = await s3_service.get_presigned_url(uploaded_key, expiration=86400)  # 24 hours

                if not s3_url:
                    logger.error(f"Failed to generate presigned URL for {file.filename}")
                    continue

                # Insert into database
                attachment_id = await conn.fetchval("""
                    INSERT INTO salary_attachments (
                        id, tenant_id, salary_payment_id,
                        path, file_name, file_size, mime_type, s3_key,
                        uploaded_by
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id
                """,
                    uuid4(), tenant_id, payment_id,
                    s3_url, file.filename, file.size, file.content_type, uploaded_key,
                    user_id
                )

                uploaded_files.append({
                    'id': str(attachment_id),
                    'fileName': file.filename,
                    'fileSize': file.size,
                    'mimeType': file.content_type,
                    's3Url': s3_url
                })

            except Exception as e:
                logger.error(f"Error uploading file {file.filename}: {e}")
                continue

        logger.info(f"Uploaded {len(uploaded_files)} attachments to payment {payment_id}")

        return {
            'success': True,
            'uploaded': len(uploaded_files),
            'files': uploaded_files
        }


async def delete_salary_payment_attachment(
    request: Request,
    attachment_id: UUID
) -> Dict[str, Any]:
    """
    Delete attachment from salary payment
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Fetch attachment with tenant validation
        attachment = await conn.fetchrow("""
            SELECT id, s3_key
            FROM salary_attachments
            WHERE id = $1 AND tenant_id = $2
        """, attachment_id, tenant_id)

        if not attachment:
            raise NotFoundError("Attachment not found")

        # Delete from S3
        if attachment['s3_key']:
            s3_service = AWSS3Service()
            try:
                await s3_service.delete_file(attachment['s3_key'])
            except Exception as e:
                logger.error(f"Error deleting S3 file: {e}")

        # Delete from database
        await conn.execute(
            "DELETE FROM salary_attachments WHERE id = $1",
            attachment_id
        )

        logger.info(f"Deleted attachment {attachment_id}")

        return {'success': True, 'message': 'Attachment deleted successfully'}
