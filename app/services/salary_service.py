"""
Salary Service
Handles employee salary configuration and payment management
"""
import logging
import json
from fastapi import Request, UploadFile
from typing import List, Optional, Dict, Any
from uuid import UUID, uuid4
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
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
    SalaryAttachment,
    PrimaPayment,
    PrimaPaymentCreate,
    PrimaPaymentResponse,
    PrimaPaymentListResponse,
    CesantiasPayment,
    CesantiasPaymentCreate,
    CesantiasPaymentResponse,
    CesantiasPaymentListResponse,
    IntCesantiasPayment,
    IntCesantiasPaymentCreate,
    IntCesantiasPaymentResponse,
    IntCesantiasPaymentListResponse,
    VacacionesPayment,
    VacacionesPaymentCreate,
    VacacionesPaymentResponse,
    VacacionesPaymentListResponse,
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
    withholding_amount: Optional[Decimal] = None,
    net_pay: Optional[Decimal] = None,
    employee_ss: Optional[Decimal] = None,
) -> None:
    """
    Post a double-entry GL journal entry for a salary payment.

    Without withholding or SS (2 lines):
      DR: 5105 (employee/daily) or 5199 (contractor)  — gross
      CR: 1105/1110 (cash/bank)                        — gross

    With withholding (3 lines, contractors only):
      DR: 5199 Honorarios                              — gross
      CR: 1110 Bancos                                  — net (gross − withholding)
      CR: 2367 Retención en la fuente por pagar        — withholding

    With SS deductions (3 lines, employees/daily):
      DR: 5105 Sueldos                                 — gross
      CR: 1110 Bancos                                  — net_pay (gross − employee_ss)
      CR: 237005 Aportes SS empleado (EPS + AFP)       — employee_ss

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

    gross_amt = float(payment_amount)
    use_withholding = withholding_amount and withholding_amount > 0
    use_ss = net_pay is not None and employee_ss is not None and employee_ss > 0

    if use_withholding:
        wh_amt = float(withholding_amount or 0)
        net_amt = gross_amt - wh_amt

        # Resolve account 2367 — Retención en la fuente por pagar
        wh_acct = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '2367' AND is_active = true",
            tenant_id,
        )
        if not wh_acct:
            logger.warning(f"[GL] Withholding account 2367 not found for tenant {tenant_id} — posting without withholding split")
            use_withholding = False

    if use_ss:
        ss_acct = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '237005' AND is_active = true",
            tenant_id,
        )
        if not ss_acct:
            logger.warning(f"[GL] SS account 237005 not found for tenant {tenant_id} — posting gross without SS split")
            use_ss = False

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            description, payment_id, gross_amt, gross_amt,
        )
        entry_id = entry_row["id"]

        # Debit line — expense account (always gross)
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, debit_acct["id"], gross_amt, description,
        )

        if use_withholding:
            # Credit line 1 — cash/bank (net amount)
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, 1)""",
                entry_id, credit_acct["id"], net_amt, description,
            )
            # Credit line 2 — 2367 Retención en la fuente por pagar
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, 2)""",
                entry_id, wh_acct["id"], wh_amt, f"Retefuente — {description}",
            )
            logger.info(f"[GL] Posted salary entry {entry_id} for payment {payment_id} (gross={gross_amt}, net={net_amt}, wh={wh_amt}, debit={debit_code}, credit={credit_code}, wh_acct=2367)")
        elif use_ss:
            net_amt_ss = float(net_pay)
            ss_emp_amt = float(employee_ss)
            # Credit line 1 — cash/bank (net: gross − employee SS)
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, 1)""",
                entry_id, credit_acct["id"], net_amt_ss, description,
            )
            # Credit line 2 — 2370 employee SS deduction
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, 2)""",
                entry_id, ss_acct["id"], ss_emp_amt, f"SS empleado — {description}",
            )
            logger.info(f"[GL] Posted salary entry {entry_id} for payment {payment_id} (gross={gross_amt}, net={net_amt_ss}, ss_emp={ss_emp_amt}, debit={debit_code}, credit={credit_code})")
        else:
            # Credit line — cash/bank (full amount)
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, 1)""",
                entry_id, credit_acct["id"], gross_amt, description,
            )
            logger.info(f"[GL] Posted salary entry {entry_id} for payment {payment_id} (amount={gross_amt}, debit={debit_code}, credit={credit_code})")


async def _post_provision_gl_entries(
    conn,
    tenant_id: UUID,
    payment_id: UUID,
    period_month: str,
    employment_type: Optional[str],
    gross_salary: Decimal,
) -> None:
    """
    Post monthly social benefit provision GL entries after a salary payment.

    DR 5106 Gasto prima de servicios         CR 2620 Prima de servicios
    DR 5107 Gasto cesantías                  CR 2610 Cesantías consolidadas
    DR 5108 Gasto intereses sobre cesantías  CR 2615 Intereses sobre cesantías
    DR 5109 Gasto vacaciones                 CR 2625 Vacaciones consolidadas

    Graceful degrade — never raises, never blocks the payment flow.
    Skips for 'contractor' employment type.
    Caller MUST wrap in try/except.
    """
    if employment_type == "contractor":
        return
    if gross_salary <= 0:
        return

    # Idempotency: skip if provision GL already posted for this payment
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_provision' AND source_id = $2""",
        tenant_id, payment_id,
    )
    if already_posted:
        logger.info(f"[provision_gl] Provision GL for payment {payment_id} already posted — skip")
        return

    # Calculate provisions
    prima      = (gross_salary / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cesantias  = (gross_salary / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vacaciones = (gross_salary / 24).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # YTD cesantías for interest calc: sum cesantias from salary_provisions for same tenant in current calendar year
    year = period_month[:4]
    ytd_row = await conn.fetchval(
        """SELECT COALESCE(SUM(cesantias), 0)
           FROM salary_provisions
           WHERE tenant_id = $1 AND period_month LIKE $2""",
        tenant_id, f"{year}-%",
    )
    ytd_cesantias = Decimal(str(ytd_row)) if ytd_row else Decimal("0")
    int_cesantias = ((ytd_cesantias + cesantias) * Decimal("0.01") / 12).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    # Upsert salary_provisions row (persists even if GL fails)
    existing_prov = await conn.fetchval(
        "SELECT 1 FROM salary_provisions WHERE payment_id = $1",
        payment_id,
    )
    if not existing_prov:
        await conn.execute(
            """INSERT INTO salary_provisions
                   (tenant_id, payment_id, period_month, employment_type,
                    gross_salary, prima, cesantias, int_cesantias, vacaciones)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (payment_id) DO NOTHING""",
            tenant_id, payment_id, period_month,
            employment_type or "employee",
            gross_salary, prima, cesantias, int_cesantias, vacaciones,
        )

    # Check accounting period is open
    today = date.today()
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, today.year, today.month,
    )
    if closed:
        logger.warning(
            f"[provision_gl] Period {today.year}-{today.month:02d} closed — skip GL for provision on payment {payment_id}"
        )
        return

    # Resolve GL accounts — expense (DR) and provision (CR)
    # pairs: (dr_code, cr_code, amount, description)
    provision_pairs = [
        ("5106", "2620", prima,         "Provisión prima de servicios"),
        ("5107", "2610", cesantias,     "Provisión cesantías"),
        ("5108", "2615", int_cesantias, "Provisión intereses sobre cesantías"),
        ("5109", "2625", vacaciones,    "Provisión vacaciones"),
    ]

    lines = []
    for dr_code, cr_code, amount, description in provision_pairs:
        if amount <= 0:
            continue
        dr_acct = await conn.fetchval(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, dr_code,
        )
        cr_acct = await conn.fetchval(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, cr_code,
        )
        if not dr_acct or not cr_acct:
            logger.warning(
                f"[provision_gl] Missing account {dr_code} or {cr_code} for tenant {tenant_id} — skip {description}"
            )
            continue
        lines.append((dr_acct, cr_acct, float(amount), description))

    if not lines:
        return

    total_debit = sum(l[2] for l in lines)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_provision', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, today, today.year, today.month,
            f"Provisión prestaciones sociales — {period_month}",
            payment_id, total_debit, total_debit,
        )
        entry_id = entry_row["id"]

        line_order = 0
        for dr_acct, cr_acct, amount, description in lines:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, 0, $4, $5)""",
                entry_id, dr_acct, amount, description, line_order,
            )
            line_order += 1
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, cr_acct, amount, description, line_order,
            )
            line_order += 1

    logger.info(
        f"[provision_gl] Posted provision entry {entry_id} for payment {payment_id} "
        f"(prima={prima}, cesantias={cesantias}, int_cesantias={int_cesantias}, vacaciones={vacaciones})"
    )


async def _post_ss_gl_entry(
    conn,
    tenant_id: UUID,
    payment_id: UUID,
    payment_date,
    employee_health: Decimal,
    employee_pension: Decimal,
    employer_health: Decimal,
    employer_pension: Decimal,
    employer_arl: Decimal,
    employer_caja: Decimal,
    description: str,
) -> None:
    """
    Post SS/parafiscales GL entry for a salary payment.

    Entry 1 — source_module='nomina_ss' (employer contributions):
      DR 5120   Aportes EPS/ARL/AFP/Caja              employer_total
      CR 237010 Aportes SS empleador (EPS+AFP+ARL+Caja) employer_total

    Employee deductions (employee_health + employee_pension) are credited to 237005
    via the main salary entry — see _post_salary_gl_entry with net_pay.

    Graceful degrade — never raises, never blocks payment.
    """
    employer_total = employer_health + employer_pension + employer_arl + employer_caja
    if employer_total <= 0:
        return

    # Idempotency: skip if already posted for this payment
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_ss' AND source_id = $2 AND status = 'posted'""",
        tenant_id, payment_id
    )
    if already_posted:
        logger.info(f"[ss_gl] SS entry for payment {payment_id} already posted — skip")
        return

    # Check period open
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else payment_date
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(f"[ss_gl] Period closed — skip SS GL for payment {payment_id}")
        return

    # Resolve accounts
    debit_acct = await conn.fetchval(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '5120' AND is_active = true",
        tenant_id,
    )
    credit_acct = await conn.fetchval(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '237010' AND is_active = true",
        tenant_id,
    )
    if not debit_acct or not credit_acct:
        logger.warning(f"[ss_gl] Missing account 5120 or 237010 for tenant {tenant_id} — skip SS GL")
        return

    emp_total = float(employer_total)
    ss_description = f"Aportes SS empleador — {description}"

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_ss', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            ss_description, payment_id, emp_total, emp_total,
        )
        entry_id = entry_row["id"]

        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, debit_acct, emp_total, ss_description,
        )
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, credit_acct, emp_total, ss_description,
        )

    logger.info(
        f"[ss_gl] Posted SS entry {entry_id} for payment {payment_id} "
        f"(employer: health={employer_health}, pension={employer_pension}, arl={employer_arl}, caja={employer_caja})"
    )


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
                es.arl_rate,
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
                arl_rate=Decimal(str(row['arl_rate'])) if row.get('arl_rate') else None,
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
                       file_size, mime_type, s3_key, uploaded_by, uploaded_at, label
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
                    uploaded_at=a['uploaded_at'],
                    label=a['label']
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
        if config.employment_type == 'contractor' and str(config.salary_type) == 'smmlv':
            raise ValidationError("Contractors cannot use SMMLV salary type — use fixed or hourly")

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

        # Fetch salary config to get employment_type, daily_rate, and arl_rate
        salary_config = await conn.fetchrow(
            """SELECT employment_type, daily_rate, arl_rate FROM employee_salaries
               WHERE tenant_member_id = $1 ORDER BY period_month DESC LIMIT 1""",
            payment_data.tenant_member_id
        )
        employment_type = salary_config['employment_type'] if salary_config else 'employee'
        daily_rate = Decimal(str(salary_config['daily_rate'])) if salary_config and salary_config['daily_rate'] else None

        # Auto-calculate amount for daily workers
        payment_amount = payment_data.payment_amount
        if employment_type == 'daily' and payment_data.days_worked and daily_rate:
            payment_amount = Decimal(str(payment_data.days_worked)) * daily_rate

        # Resolve withholding fields (only valid for contractors)
        withholding_rate = getattr(payment_data, 'withholding_rate', None)
        withholding_amount = getattr(payment_data, 'withholding_amount', None)
        if employment_type != 'contractor':
            withholding_rate = None
            withholding_amount = None

        # Calculate SS/parafiscales (employees and daily workers, when ss_enabled)
        ss_enabled = getattr(payment_data, 'ss_enabled', False) and employment_type != 'contractor'
        Q = Decimal('0.01')
        employee_health = Decimal('0')
        employee_pension = Decimal('0')
        employer_health = Decimal('0')
        employer_pension = Decimal('0')
        employer_arl = Decimal('0')
        employer_caja = Decimal('0')
        net_pay = None

        if ss_enabled:
            arl_rate = getattr(payment_data, 'arl_rate', None)
            if arl_rate is None and salary_config and salary_config.get('arl_rate'):
                arl_rate = Decimal(str(salary_config['arl_rate']))
            if arl_rate is None:
                arl_rate = Decimal('0.005220')

            caja_rate = getattr(payment_data, 'caja_rate', None)
            if caja_rate is None:
                caja_rate = Decimal('0.04')

            employee_health  = (payment_amount * Decimal('0.04')).quantize(Q)
            employee_pension = (payment_amount * Decimal('0.04')).quantize(Q)
            employer_health  = (payment_amount * Decimal('0.085')).quantize(Q)
            employer_pension = (payment_amount * Decimal('0.12')).quantize(Q)
            employer_arl     = (payment_amount * arl_rate).quantize(Q)
            employer_caja    = (payment_amount * caja_rate).quantize(Q)
            net_pay          = payment_amount - employee_health - employee_pension

        # Create payment
        payment_id = uuid4()
        insert_query = """
            INSERT INTO salary_payments (
                id, tenant_id, tenant_member_id, period_month,
                payment_amount, payment_method, payment_reference,
                payment_date, notes, status, days_worked,
                withholding_rate, withholding_amount, net_pay,
                created_by, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, NOW(), NOW())
            RETURNING id, tenant_id, tenant_member_id, period_month,
                      payment_amount, payment_method, payment_reference,
                      payment_date, notes, status, days_worked,
                      withholding_rate, withholding_amount, net_pay,
                      created_by, created_at, updated_at
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
            withholding_rate,
            withholding_amount,
            net_pay,
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
            employee_ss = (employee_health + employee_pension) if ss_enabled else None
            await _post_salary_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                payment_id=payment_id,
                payment_date=payment_data.payment_date,
                payment_amount=payment_amount,
                employment_type=employment_type,
                payment_method=str(payment_data.payment_method) if payment_data.payment_method else None,
                description=gl_description,
                withholding_amount=withholding_amount,
                net_pay=net_pay,
                employee_ss=employee_ss,
            )
        except Exception as gl_err:
            logger.warning(f"[GL] Salary GL posting failed for payment {payment_id}: {gl_err}")

        # Post social benefit provisions (graceful degrade — never blocks payment)
        try:
            await _post_provision_gl_entries(
                conn=conn,
                tenant_id=tenant_id,
                payment_id=payment_id,
                period_month=payment_data.period_month,
                employment_type=employment_type,
                gross_salary=payment_amount,
            )
        except Exception as prov_err:
            logger.warning(f"[provision_gl] Provision GL posting failed for payment {payment_id}: {prov_err}")

        # Post SS employer contributions GL (graceful degrade — never blocks payment)
        if ss_enabled:
            try:
                await _post_ss_gl_entry(
                    conn=conn,
                    tenant_id=tenant_id,
                    payment_id=payment_id,
                    payment_date=payment_data.payment_date,
                    employee_health=employee_health,
                    employee_pension=employee_pension,
                    employer_health=employer_health,
                    employer_pension=employer_pension,
                    employer_arl=employer_arl,
                    employer_caja=employer_caja,
                    description=gl_description,
                )
                # Store SS amounts in salary_provisions
                await conn.execute(
                    """UPDATE salary_provisions SET
                           employee_health=$1, employee_pension=$2,
                           employer_health=$3, employer_pension=$4,
                           employer_arl=$5, employer_caja=$6, net_pay=$7
                       WHERE payment_id=$8""",
                    employee_health, employee_pension,
                    employer_health, employer_pension,
                    employer_arl, employer_caja, net_pay,
                    payment_id,
                )
            except Exception as ss_err:
                logger.warning(f"[ss_gl] SS GL posting failed for payment {payment_id}: {ss_err}")

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
            net_pay=Decimal(str(row['net_pay'])) if row.get('net_pay') is not None else None,
            withholding_rate=Decimal(str(row['withholding_rate'])) if row['withholding_rate'] is not None else None,
            withholding_amount=Decimal(str(row['withholding_amount'])) if row['withholding_amount'] is not None else None,
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
    withholding_rate: Optional[Decimal] = None,
    withholding_amount: Optional[Decimal] = None,
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

        # Withholding only applies to contractors
        mp_withholding_rate = withholding_rate if mp_employment_type == 'contractor' else None
        mp_withholding_amount = withholding_amount if mp_employment_type == 'contractor' else None

        # Create payment
        payment_id = uuid4()
        insert_query = """
            INSERT INTO salary_payments (
                id, tenant_id, tenant_member_id, period_month,
                payment_amount, payment_method, payment_reference,
                payment_date, notes, days_worked,
                withholding_rate, withholding_amount,
                created_by, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW(), NOW())
            RETURNING id, tenant_id, tenant_member_id, period_month,
                      payment_amount, payment_method, payment_reference,
                      payment_date, notes, days_worked,
                      withholding_rate, withholding_amount,
                      created_by, created_at, updated_at
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
            mp_withholding_rate,
            mp_withholding_amount,
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
                withholding_amount=mp_withholding_amount,
            )
        except Exception as gl_err:
            logger.warning(f"[GL] Salary GL posting failed for payment {payment_id}: {gl_err}")

        # Post social benefit provisions (graceful degrade — never blocks payment)
        try:
            await _post_provision_gl_entries(
                conn=conn,
                tenant_id=tenant_id,
                payment_id=payment_id,
                period_month=period_month,
                employment_type=mp_employment_type,
                gross_salary=actual_payment_amount,
            )
        except Exception as prov_err:
            logger.warning(f"[provision_gl] Provision GL posting failed for payment {payment_id}: {prov_err}")

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
            withholding_rate=Decimal(str(row['withholding_rate'])) if row['withholding_rate'] is not None else None,
            withholding_amount=Decimal(str(row['withholding_amount'])) if row['withholding_amount'] is not None else None,
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
                    uploaded_at=a['uploaded_at'],
                    label=a['label']
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

        # Get provisions (may not exist for contractor payments or old payments)
        prov_row = await conn.fetchrow(
            "SELECT * FROM salary_provisions WHERE payment_id = $1",
            payment_id
        )
        provisions = None
        if prov_row:
            provisions = {
                'prima':         float(prov_row['prima']),
                'cesantias':     float(prov_row['cesantias']),
                'int_cesantias': float(prov_row['int_cesantias']),
                'vacaciones':    float(prov_row['vacaciones']),
                'gross_salary':  float(prov_row['gross_salary']),
                'period_month':  prov_row['period_month'],
                'employment_type': prov_row['employment_type'],
            }

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
            'attachments': attachments,
            'provisions': provisions,
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
    files: List[UploadFile],
    label: Optional[str] = None
) -> Dict[str, Any]:
    """
    Upload attachments for existing salary payment.
    label: optional purpose tag (e.g. 'pila' for PILA/social security proof).
           PILA files are stored under salaries/{tenant_id}/pila/ subfolder.
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
                # Branch S3 path by label: PILA files go to dedicated subfolder
                if label == 'pila':
                    s3_key = f"salaries/{tenant_id}/pila/{uuid4()}_{file.filename}"
                else:
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

                # Insert into database with optional label
                attachment_id = await conn.fetchval("""
                    INSERT INTO salary_attachments (
                        id, tenant_id, salary_payment_id,
                        path, file_name, file_size, mime_type, s3_key,
                        uploaded_by, label
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING id
                """,
                    uuid4(), tenant_id, payment_id,
                    s3_url, file.filename, file.size, file.content_type, uploaded_key,
                    user_id, label
                )

                uploaded_files.append({
                    'id': str(attachment_id),
                    'fileName': file.filename,
                    'fileSize': file.size,
                    'mimeType': file.content_type,
                    's3Url': s3_url,
                    'label': label
                })

            except Exception as e:
                logger.error(f"Error uploading file {file.filename}: {e}")
                continue

        logger.info(f"Uploaded {len(uploaded_files)} attachments to payment {payment_id} (label={label})")

        return {
            'success': True,
            'uploaded': len(uploaded_files),
            'files': uploaded_files
        }


async def _post_prima_payment_gl_entry(
    conn,
    tenant_id: UUID,
    prima_payment_id: UUID,
    prima_amount: Decimal,
    payment_method: Optional[str],
    payment_date: Any,
    semestre: str,
) -> None:
    """
    Post prima de servicios disbursement GL entry.
    DR 2620 (Provisión prima de servicios) / CR bank account

    This is a balance-sheet entry — liquidates the accrued liability.
    Graceful degrade — never raises. Caller MUST wrap in try/except.
    """
    if prima_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_prima' AND source_id = $2""",
        tenant_id, prima_payment_id,
    )
    if already_posted:
        logger.info(f"[prima_gl] Prima payment {prima_payment_id} already posted — skip")
        return

    # Resolve entry date
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else (payment_date or date.today())

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(
            f"[prima_gl] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for prima payment {prima_payment_id}"
        )
        return

    # Resolve DR account: 2620 (Provisión prima de servicios) — debit liquidates liability
    dr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '2620' AND is_active = true",
        tenant_id,
    )
    if not dr_acct:
        logger.warning(f"[prima_gl] DR account 2620 not found for tenant {tenant_id} — skip prima GL")
        return

    # Resolve CR account: bank/cash (dynamic, same as salary credit resolution)
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    cr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not cr_acct:
        logger.warning(
            f"[prima_gl] CR account {credit_code} not found for tenant {tenant_id} — skip prima GL"
        )
        return

    amount_float = float(prima_amount)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_prima', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            f"Pago prima de servicios — {semestre}",
            prima_payment_id, amount_float, amount_float,
        )
        entry_id = entry_row["id"]

        # DR line — 2620 Provisión prima de servicios (debit liquidates the liability)
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, dr_acct["id"], amount_float, f"Prima de servicios {semestre}",
        )
        # CR line — bank/cash
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, cr_acct["id"], amount_float, f"Prima de servicios {semestre}",
        )

    logger.info(
        f"[prima_gl] Posted prima entry {entry_id} for payment {prima_payment_id} "
        f"(amount={amount_float}, DR=2620, CR={credit_code}, semestre={semestre})"
    )


async def record_prima_payment(
    request: Request,
    member_id: UUID,
    data: PrimaPaymentCreate,
) -> PrimaPaymentResponse:
    """Register prima de servicios payment for an employee."""
    from fastapi import HTTPException as _HTTPException
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify employee belongs to tenant
        member = await conn.fetchrow(
            "SELECT id FROM tenant_members WHERE id = $1 AND tenant_id = $2",
            member_id, tenant_id,
        )
        if not member:
            raise _HTTPException(status_code=404, detail="Employee not found")

        # Verify employment_type is 'employee' or 'daily'
        # Per Sentencia C-825/06 (Corte Constitucional), jornaleros/ocasionales are entitled
        # to prima de servicios proportionally. Only contractors are excluded.
        emp = await conn.fetchrow(
            """SELECT employment_type FROM employee_salaries
               WHERE tenant_member_id = $1 ORDER BY period_month DESC LIMIT 1""",
            member_id,
        )
        if not emp or emp['employment_type'] == 'contractor':
            raise _HTTPException(
                status_code=400,
                detail="Prima de servicios only applies to employees and daily workers (not contractors)"
            )

        # Calculate prima_amount: (gross_salary / 180) * days_worked
        days = data.days_worked if data.days_worked else 180
        prima_amount = (
            data.gross_salary / Decimal('180') * Decimal(days)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        payment_date = data.payment_date or datetime.now()

        # Insert prima_payment (unique constraint on tenant_member_id + semestre prevents duplicates)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO prima_payments
                    (tenant_id, tenant_member_id, semestre, gross_salary, days_worked,
                     prima_amount, payment_method, payment_date, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                tenant_id, member_id, data.semestre, data.gross_salary, days,
                prima_amount, data.payment_method, payment_date, data.notes,
            )
        except Exception as e:
            if 'unique' in str(e).lower():
                raise _HTTPException(
                    status_code=409,
                    detail=f"Prima for semestre {data.semestre} already paid for this employee"
                )
            raise

        payment_id = row['id']

        # Post GL entry (graceful degrade — never blocks payment)
        try:
            await _post_prima_payment_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                prima_payment_id=payment_id,
                prima_amount=prima_amount,
                payment_method=data.payment_method,
                payment_date=payment_date,
                semestre=data.semestre,
            )
        except Exception as gl_err:
            logger.warning(f"[prima_gl] GL post failed for prima payment {payment_id}: {gl_err}")

        prima = PrimaPayment(
            id=row['id'],
            tenant_id=row['tenant_id'],
            tenant_member_id=row['tenant_member_id'],
            semestre=row['semestre'],
            gross_salary=Decimal(str(row['gross_salary'])),
            days_worked=row['days_worked'],
            prima_amount=Decimal(str(row['prima_amount'])),
            payment_method=row['payment_method'],
            payment_date=row['payment_date'],
            notes=row['notes'],
            created_at=row['created_at'],
        )

        logger.info(
            f"Prima payment recorded for employee {member_id}: "
            f"semestre={data.semestre}, amount={prima_amount}"
        )

        return PrimaPaymentResponse(success=True, data=prima)


async def get_prima_payments(
    request: Request,
    member_id: UUID,
) -> PrimaPaymentListResponse:
    """List prima de servicios payments for an employee, ordered by payment_date DESC."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM prima_payments
               WHERE tenant_member_id = $1 AND tenant_id = $2
               ORDER BY payment_date DESC""",
            member_id, tenant_id,
        )

        payments: List[PrimaPayment] = [
            PrimaPayment(
                id=r['id'],
                tenant_id=r['tenant_id'],
                tenant_member_id=r['tenant_member_id'],
                semestre=r['semestre'],
                gross_salary=Decimal(str(r['gross_salary'])),
                days_worked=r['days_worked'],
                prima_amount=Decimal(str(r['prima_amount'])),
                payment_method=r['payment_method'],
                payment_date=r['payment_date'],
                notes=r['notes'],
                created_at=r['created_at'],
            )
            for r in rows
        ]

        return PrimaPaymentListResponse(success=True, data=payments)


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


# =============================================================================
# CESANTÍAS GL HELPER
# =============================================================================

async def _post_cesantias_payment_gl_entry(
    conn,
    tenant_id: UUID,
    cesantias_payment_id: UUID,
    cesantias_amount: Decimal,
    payment_method: Optional[str],
    payment_date: Any,
    anio: int,
) -> None:
    """
    Post cesantías consignation GL entry.
    DR 2610 (Cesantías consolidadas) / CR bank account

    This is a balance-sheet entry — liquidates the accrued liability.
    Graceful degrade — never raises. Caller MUST wrap in try/except.
    """
    if cesantias_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_cesantias' AND source_id = $2""",
        tenant_id, cesantias_payment_id,
    )
    if already_posted:
        logger.info(f"[cesantias_gl] Cesantias payment {cesantias_payment_id} already posted — skip")
        return

    # Resolve entry date
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else (payment_date or date.today())

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(
            f"[cesantias_gl] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for cesantias payment {cesantias_payment_id}"
        )
        return

    # Resolve DR account: 2610 (Cesantías consolidadas) — debit liquidates liability
    dr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '2610' AND is_active = true",
        tenant_id,
    )
    if not dr_acct:
        logger.warning(f"[cesantias_gl] DR account 2610 not found for tenant {tenant_id} — skip cesantias GL")
        return

    # Resolve CR account: bank/cash
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    cr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not cr_acct:
        logger.warning(
            f"[cesantias_gl] CR account {credit_code} not found for tenant {tenant_id} — skip cesantias GL"
        )
        return

    amount_float = float(cesantias_amount)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_cesantias', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            f"Consignación cesantías — {anio}",
            cesantias_payment_id, amount_float, amount_float,
        )
        entry_id = entry_row["id"]

        # DR line — 2610 Cesantías consolidadas (debit liquidates the liability)
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, dr_acct["id"], amount_float, f"Cesantías {anio}",
        )
        # CR line — bank/cash
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, cr_acct["id"], amount_float, f"Cesantías {anio}",
        )

    logger.info(
        f"[cesantias_gl] Posted cesantias entry {entry_id} for payment {cesantias_payment_id} "
        f"(amount={amount_float}, DR=2610, CR={credit_code}, anio={anio})"
    )


# =============================================================================
# INTERESES SOBRE CESANTÍAS GL HELPER
# =============================================================================

async def _post_int_cesantias_payment_gl_entry(
    conn,
    tenant_id: UUID,
    int_cesantias_payment_id: UUID,
    int_cesantias_amount: Decimal,
    payment_method: Optional[str],
    payment_date: Any,
    anio: int,
) -> None:
    """
    Post intereses sobre cesantías GL entry.
    DR 2615 (Intereses sobre cesantías) / CR bank account

    This is a balance-sheet entry — liquidates the accrued liability.
    Graceful degrade — never raises. Caller MUST wrap in try/except.
    """
    if int_cesantias_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_int_cesantias' AND source_id = $2""",
        tenant_id, int_cesantias_payment_id,
    )
    if already_posted:
        logger.info(f"[int_cesantias_gl] Int cesantias payment {int_cesantias_payment_id} already posted — skip")
        return

    # Resolve entry date
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else (payment_date or date.today())

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(
            f"[int_cesantias_gl] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for int_cesantias payment {int_cesantias_payment_id}"
        )
        return

    # Resolve DR account: 2615 (Intereses sobre cesantías) — debit liquidates liability
    dr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '2615' AND is_active = true",
        tenant_id,
    )
    if not dr_acct:
        logger.warning(f"[int_cesantias_gl] DR account 2615 not found for tenant {tenant_id} — skip int_cesantias GL")
        return

    # Resolve CR account: bank/cash
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    cr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not cr_acct:
        logger.warning(
            f"[int_cesantias_gl] CR account {credit_code} not found for tenant {tenant_id} — skip int_cesantias GL"
        )
        return

    amount_float = float(int_cesantias_amount)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_int_cesantias', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            f"Pago intereses sobre cesantías — {anio}",
            int_cesantias_payment_id, amount_float, amount_float,
        )
        entry_id = entry_row["id"]

        # DR line — 2615 Intereses sobre cesantías (debit liquidates the liability)
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, dr_acct["id"], amount_float, f"Intereses sobre cesantías {anio}",
        )
        # CR line — bank/cash
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, cr_acct["id"], amount_float, f"Intereses sobre cesantías {anio}",
        )

    logger.info(
        f"[int_cesantias_gl] Posted int_cesantias entry {entry_id} for payment {int_cesantias_payment_id} "
        f"(amount={amount_float}, DR=2615, CR={credit_code}, anio={anio})"
    )


# =============================================================================
# CESANTÍAS SERVICE FUNCTIONS
# =============================================================================

async def record_cesantias_payment(
    request: Request,
    member_id: UUID,
    data: CesantiasPaymentCreate,
) -> CesantiasPaymentResponse:
    """Register cesantías consignation payment for an employee."""
    from fastapi import HTTPException as _HTTPException
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify employee belongs to tenant
        member = await conn.fetchrow(
            "SELECT id FROM tenant_members WHERE id = $1 AND tenant_id = $2",
            member_id, tenant_id,
        )
        if not member:
            raise _HTTPException(status_code=404, detail="Employee not found")

        # Verify employment_type is 'employee' or 'daily'
        emp = await conn.fetchrow(
            """SELECT employment_type FROM employee_salaries
               WHERE tenant_member_id = $1 ORDER BY period_month DESC LIMIT 1""",
            member_id,
        )
        if not emp or emp['employment_type'] not in ('employee', 'daily'):
            raise _HTTPException(
                status_code=400,
                detail="Cesantías only apply to employees and daily workers (employment_type='employee' or 'daily')"
            )

        # Calculate cesantias_amount: (gross_salary / 360) * days_worked
        days = data.days_worked if data.days_worked else 360
        cesantias_amount = (
            data.gross_salary / Decimal('360') * Decimal(days)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        payment_date = data.payment_date or datetime.now()

        # Insert cesantias_payment (unique constraint on tenant_member_id + anio prevents duplicates)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO cesantias_payments
                    (tenant_id, tenant_member_id, anio, gross_salary, days_worked,
                     cesantias_amount, fondo_name, payment_method, payment_date, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                tenant_id, member_id, data.anio, data.gross_salary, days,
                cesantias_amount, data.fondo_name, data.payment_method, payment_date, data.notes,
            )
        except Exception as e:
            if 'unique' in str(e).lower():
                raise _HTTPException(
                    status_code=409,
                    detail=f"Cesantías for year {data.anio} already paid for this employee"
                )
            raise

        payment_id = row['id']

        # Post GL entry (graceful degrade — never blocks payment)
        try:
            await _post_cesantias_payment_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                cesantias_payment_id=payment_id,
                cesantias_amount=cesantias_amount,
                payment_method=data.payment_method,
                payment_date=payment_date,
                anio=data.anio,
            )
        except Exception as gl_err:
            logger.warning(f"[cesantias_gl] GL post failed for cesantias payment {payment_id}: {gl_err}")

        cesantias = CesantiasPayment(
            id=row['id'],
            tenant_id=row['tenant_id'],
            tenant_member_id=row['tenant_member_id'],
            anio=row['anio'],
            gross_salary=Decimal(str(row['gross_salary'])),
            days_worked=row['days_worked'],
            cesantias_amount=Decimal(str(row['cesantias_amount'])),
            fondo_name=row['fondo_name'],
            payment_method=row['payment_method'],
            payment_date=row['payment_date'],
            notes=row['notes'],
            created_at=row['created_at'],
        )

        logger.info(
            f"Cesantias payment recorded for employee {member_id}: "
            f"anio={data.anio}, amount={cesantias_amount}"
        )

        return CesantiasPaymentResponse(success=True, data=cesantias)


async def get_cesantias_payments(
    request: Request,
    member_id: UUID,
) -> CesantiasPaymentListResponse:
    """List cesantías payments for an employee, ordered by anio DESC."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM cesantias_payments
               WHERE tenant_member_id = $1 AND tenant_id = $2
               ORDER BY anio DESC""",
            member_id, tenant_id,
        )

        payments: List[CesantiasPayment] = [
            CesantiasPayment(
                id=r['id'],
                tenant_id=r['tenant_id'],
                tenant_member_id=r['tenant_member_id'],
                anio=r['anio'],
                gross_salary=Decimal(str(r['gross_salary'])),
                days_worked=r['days_worked'],
                cesantias_amount=Decimal(str(r['cesantias_amount'])),
                fondo_name=r['fondo_name'],
                payment_method=r['payment_method'],
                payment_date=r['payment_date'],
                notes=r['notes'],
                created_at=r['created_at'],
            )
            for r in rows
        ]

        return CesantiasPaymentListResponse(success=True, data=payments)


# =============================================================================
# INTERESES SOBRE CESANTÍAS SERVICE FUNCTIONS
# =============================================================================

async def record_int_cesantias_payment(
    request: Request,
    member_id: UUID,
    data: IntCesantiasPaymentCreate,
) -> IntCesantiasPaymentResponse:
    """Register intereses sobre cesantías payment for an employee."""
    from fastapi import HTTPException as _HTTPException
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify employee belongs to tenant
        member = await conn.fetchrow(
            "SELECT id FROM tenant_members WHERE id = $1 AND tenant_id = $2",
            member_id, tenant_id,
        )
        if not member:
            raise _HTTPException(status_code=404, detail="Employee not found")

        # Verify employment_type is 'employee' or 'daily'
        emp = await conn.fetchrow(
            """SELECT employment_type FROM employee_salaries
               WHERE tenant_member_id = $1 ORDER BY period_month DESC LIMIT 1""",
            member_id,
        )
        if not emp or emp['employment_type'] not in ('employee', 'daily'):
            raise _HTTPException(
                status_code=400,
                detail="Intereses sobre cesantías only apply to employees and daily workers (employment_type='employee' or 'daily')"
            )

        # Calculate int_cesantias_amount: base * 0.12, or use provided value
        if data.int_cesantias_amount is not None:
            int_cesantias_amount = data.int_cesantias_amount.quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        else:
            int_cesantias_amount = (
                data.cesantias_base * Decimal('0.12')
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        payment_date = data.payment_date or datetime.now()

        # Insert int_cesantias_payment (unique constraint on tenant_member_id + anio prevents duplicates)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO int_cesantias_payments
                    (tenant_id, tenant_member_id, anio, cesantias_base,
                     int_cesantias_amount, payment_method, payment_date, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                tenant_id, member_id, data.anio, data.cesantias_base,
                int_cesantias_amount, data.payment_method, payment_date, data.notes,
            )
        except Exception as e:
            if 'unique' in str(e).lower():
                raise _HTTPException(
                    status_code=409,
                    detail=f"Intereses sobre cesantías for year {data.anio} already paid for this employee"
                )
            raise

        payment_id = row['id']

        # Post GL entry (graceful degrade — never blocks payment)
        try:
            await _post_int_cesantias_payment_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                int_cesantias_payment_id=payment_id,
                int_cesantias_amount=int_cesantias_amount,
                payment_method=data.payment_method,
                payment_date=payment_date,
                anio=data.anio,
            )
        except Exception as gl_err:
            logger.warning(f"[int_cesantias_gl] GL post failed for int_cesantias payment {payment_id}: {gl_err}")

        int_cesantias = IntCesantiasPayment(
            id=row['id'],
            tenant_id=row['tenant_id'],
            tenant_member_id=row['tenant_member_id'],
            anio=row['anio'],
            cesantias_base=Decimal(str(row['cesantias_base'])),
            int_cesantias_amount=Decimal(str(row['int_cesantias_amount'])),
            payment_method=row['payment_method'],
            payment_date=row['payment_date'],
            notes=row['notes'],
            created_at=row['created_at'],
        )

        logger.info(
            f"Int cesantias payment recorded for employee {member_id}: "
            f"anio={data.anio}, amount={int_cesantias_amount}"
        )

        return IntCesantiasPaymentResponse(success=True, data=int_cesantias)


async def get_int_cesantias_payments(
    request: Request,
    member_id: UUID,
) -> IntCesantiasPaymentListResponse:
    """List intereses sobre cesantías payments for an employee, ordered by anio DESC."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM int_cesantias_payments
               WHERE tenant_member_id = $1 AND tenant_id = $2
               ORDER BY anio DESC""",
            member_id, tenant_id,
        )

        payments: List[IntCesantiasPayment] = [
            IntCesantiasPayment(
                id=r['id'],
                tenant_id=r['tenant_id'],
                tenant_member_id=r['tenant_member_id'],
                anio=r['anio'],
                cesantias_base=Decimal(str(r['cesantias_base'])),
                int_cesantias_amount=Decimal(str(r['int_cesantias_amount'])),
                payment_method=r['payment_method'],
                payment_date=r['payment_date'],
                notes=r['notes'],
                created_at=r['created_at'],
            )
            for r in rows
        ]

        return IntCesantiasPaymentListResponse(success=True, data=payments)


# =============================================================================
# VACACIONES GL HELPER
# =============================================================================

async def _post_vacaciones_payment_gl_entry(
    conn,
    tenant_id: UUID,
    vacaciones_payment_id: UUID,
    vacaciones_amount: Decimal,
    payment_method: Optional[str],
    payment_date: Any,
    anio: int,
) -> None:
    """
    Post vacaciones cash compensation GL entry.
    DR 2625 (Vacaciones consolidadas) / CR bank account

    This is a balance-sheet entry — liquidates the accrued liability.
    Graceful degrade — never raises. Caller MUST wrap in try/except.
    """
    if vacaciones_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_vacaciones' AND source_id = $2""",
        tenant_id, vacaciones_payment_id,
    )
    if already_posted:
        logger.info(f"[vacaciones_gl] Payment {vacaciones_payment_id} already posted — skip")
        return

    # Resolve entry date
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else (payment_date or date.today())

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(
            f"[vacaciones_gl] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for vacaciones payment {vacaciones_payment_id}"
        )
        return

    # Resolve DR account: 2625 (Vacaciones consolidadas) — debit liquidates liability
    dr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '2625' AND is_active = true",
        tenant_id,
    )
    if not dr_acct:
        logger.warning(f"[vacaciones_gl] DR account 2625 not found for tenant {tenant_id} — skip GL")
        return

    # Resolve CR account: bank/cash
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    cr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not cr_acct:
        logger.warning(
            f"[vacaciones_gl] CR account {credit_code} not found for tenant {tenant_id} — skip GL"
        )
        return

    amount_float = float(vacaciones_amount)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_vacaciones', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            f"Compensación vacaciones — {anio}",
            vacaciones_payment_id, amount_float, amount_float,
        )
        entry_id = entry_row["id"]

        # DR line — 2625 Vacaciones consolidadas (debit liquidates the liability)
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, dr_acct["id"], amount_float, f"Vacaciones {anio}",
        )
        # CR line — bank/cash
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, cr_acct["id"], amount_float, f"Vacaciones {anio}",
        )

    logger.info(
        f"[vacaciones_gl] Posted entry {entry_id} for payment {vacaciones_payment_id} "
        f"(amount={amount_float}, DR=2625, CR={credit_code}, anio={anio})"
    )


# =============================================================================
# VACACIONES SERVICE FUNCTIONS
# =============================================================================

async def record_vacaciones_payment(
    request: Request,
    member_id: UUID,
    data: VacacionesPaymentCreate,
) -> VacacionesPaymentResponse:
    """Register vacation cash compensation payment for an employee or daily worker."""
    from fastapi import HTTPException as _HTTPException
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify employment type — only employee and daily are eligible
        member = await conn.fetchrow(
            """SELECT es.employment_type
               FROM employee_salaries es
               JOIN tenant_members tm ON tm.id = $1
               WHERE es.tenant_member_id = $1 AND es.tenant_id = $2
               ORDER BY es.created_at DESC
               LIMIT 1""",
            member_id, tenant_id,
        )
        if not member:
            raise _HTTPException(status_code=404, detail="Employee salary config not found")

        employment_type = member["employment_type"]
        if employment_type not in ("employee", "daily"):
            raise _HTTPException(
                status_code=400,
                detail="Vacation payment only applies to employees and daily workers (not contractors)",
            )

        # Calculate vacaciones_amount: (gross_salary * days_worked) / 720
        # 720 = 360 days/year * 2 (since 15 days of vacation per 360 days worked)
        days = data.days_worked if data.days_worked else 360
        vacaciones_amount = (
            data.gross_salary * Decimal(days) / Decimal("720")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        payment_date = data.payment_date or datetime.now()

        try:
            row = await conn.fetchrow(
                """INSERT INTO vacaciones_payments
                       (tenant_id, tenant_member_id, anio, gross_salary, days_worked,
                        vacaciones_amount, payment_method, payment_date, notes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   RETURNING *""",
                tenant_id, member_id, data.anio, data.gross_salary, days,
                vacaciones_amount, data.payment_method, payment_date, data.notes,
            )
        except Exception as e:
            if "unique" in str(e).lower() or "ux_vacaciones" in str(e).lower():
                raise _HTTPException(
                    status_code=409,
                    detail=f"Vacation payment for year {data.anio} already paid for this employee",
                )
            raise

        payment_id = row["id"]

        # Post GL entry (graceful degrade)
        try:
            await _post_vacaciones_payment_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                vacaciones_payment_id=payment_id,
                vacaciones_amount=vacaciones_amount,
                payment_method=data.payment_method,
                payment_date=payment_date,
                anio=data.anio,
            )
        except Exception as e:
            logger.error(f"[vacaciones_gl] GL post failed for payment {payment_id}: {e}")

        return VacacionesPaymentResponse(
            success=True,
            data=VacacionesPayment(
                id=row["id"],
                tenant_id=row["tenant_id"],
                tenant_member_id=row["tenant_member_id"],
                anio=row["anio"],
                gross_salary=Decimal(str(row["gross_salary"])),
                days_worked=row["days_worked"],
                vacaciones_amount=Decimal(str(row["vacaciones_amount"])),
                payment_method=row["payment_method"],
                payment_date=row["payment_date"],
                notes=row["notes"],
                created_at=row["created_at"],
            ),
        )


async def get_vacaciones_payments(
    request: Request,
    member_id: UUID,
) -> VacacionesPaymentListResponse:
    """List vacation payments for an employee, ordered by anio DESC."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM vacaciones_payments
               WHERE tenant_member_id = $1 AND tenant_id = $2
               ORDER BY anio DESC""",
            member_id, tenant_id,
        )

        payments: List[VacacionesPayment] = [
            VacacionesPayment(
                id=r["id"],
                tenant_id=r["tenant_id"],
                tenant_member_id=r["tenant_member_id"],
                anio=r["anio"],
                gross_salary=Decimal(str(r["gross_salary"])),
                days_worked=r["days_worked"],
                vacaciones_amount=Decimal(str(r["vacaciones_amount"])),
                payment_method=r["payment_method"],
                payment_date=r["payment_date"],
                notes=r["notes"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

        return VacacionesPaymentListResponse(success=True, data=payments)


# =============================================================================
# DOTACIÓN PAYMENTS
# =============================================================================

async def _post_dotacion_gl_entry(
    conn,
    tenant_id: UUID,
    dotacion_payment_id: UUID,
    total_amount: Decimal,
    payment_method: Optional[str],
    payment_date: Any,
    period: str,
    year: int,
) -> None:
    """
    Post dotación GL entry.
    DR 5115 (Dotación al personal) / CR bank account

    Direct expense — no prior provision.
    Graceful degrade — never raises.
    """
    if total_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_dotacion' AND source_id = $2""",
        tenant_id, dotacion_payment_id,
    )
    if already_posted:
        logger.info(f"[dotacion_gl] Dotación payment {dotacion_payment_id} already posted — skip")
        return

    # Resolve entry date
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else (payment_date or date.today())

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(
            f"[dotacion_gl] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for dotacion {dotacion_payment_id}"
        )
        return

    # Resolve DR account: 5115 (Dotación al personal)
    dr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '5115' AND is_active = true",
        tenant_id,
    )
    if not dr_acct:
        logger.warning(f"[dotacion_gl] DR account 5115 not found for tenant {tenant_id} — skip dotacion GL")
        return

    # Resolve CR account: bank/cash
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    cr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not cr_acct:
        logger.warning(
            f"[dotacion_gl] CR account {credit_code} not found for tenant {tenant_id} — skip dotacion GL"
        )
        return

    amount_float = float(total_amount)
    period_label = {'APR': 'Apr', 'AUG': 'Ago', 'DEC': 'Dic'}.get(period, period)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_dotacion', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            f"Dotación al personal — {period_label} {year}",
            dotacion_payment_id, amount_float, amount_float,
        )
        entry_id = entry_row["id"]

        # DR line — 5115 Dotación al personal
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, dr_acct["id"], amount_float, f"Dotación {period_label} {year}",
        )
        # CR line — bank/cash
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, cr_acct["id"], amount_float, f"Dotación {period_label} {year}",
        )

    logger.info(
        f"[dotacion_gl] Posted dotacion entry {entry_id} for payment {dotacion_payment_id} "
        f"(amount={amount_float}, DR=5115, CR={credit_code}, {period} {year})"
    )


async def record_dotacion_payment(
    request: Request,
    member_id: UUID,
    data,
) -> 'DotacionPaymentResponse':
    """Register dotación (uniform/supply) payment for an eligible employee."""
    from fastapi import HTTPException as _HTTPException
    from app.models.salary import DotacionPayment, DotacionPaymentResponse
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        # Verify employment type — only 'employee' qualifies (not daily, not contractor)
        member = await conn.fetchrow(
            """SELECT es.employment_type, es.base_salary
               FROM employee_salaries es
               WHERE es.tenant_member_id = $1 AND es.tenant_id = $2
               ORDER BY es.created_at DESC
               LIMIT 1""",
            member_id, tenant_id,
        )
        if not member:
            raise _HTTPException(status_code=404, detail="Employee salary config not found")

        employment_type = member["employment_type"]
        if employment_type != "employee":
            raise _HTTPException(
                status_code=400,
                detail="Dotación only applies to employees under a labor contract (not contractors or daily workers)",
            )

        # Eligibility: base_salary <= 2 × SMMLV
        smmlv = await get_current_smmlv(conn, tenant_id)
        base_salary = Decimal(str(member["base_salary"])) if member["base_salary"] else Decimal("0")
        if base_salary > smmlv * 2:
            raise _HTTPException(
                status_code=400,
                detail=f"Employee salary ({base_salary:,.0f}) exceeds 2 × SMMLV ({smmlv * 2:,.0f}) — not eligible for dotación",
            )

        payment_date = data.payment_date or datetime.now()

        try:
            row = await conn.fetchrow(
                """INSERT INTO salary_dotacion_payments
                       (tenant_id, tenant_member_id, period, year, items_description,
                        total_amount, payment_method, payment_date, notes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   RETURNING *""",
                tenant_id, member_id, data.period, data.year, data.items_description,
                data.total_amount, data.payment_method, payment_date, data.notes,
            )
        except Exception as e:
            if "unique" in str(e).lower() or "ux_dotacion" in str(e).lower():
                raise _HTTPException(
                    status_code=409,
                    detail=f"Dotación for {data.period} {data.year} already registered for this employee",
                )
            raise

        payment_id = row["id"]

        # Post GL entry (graceful degrade)
        try:
            await _post_dotacion_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                dotacion_payment_id=payment_id,
                total_amount=data.total_amount,
                payment_method=data.payment_method,
                payment_date=payment_date,
                period=data.period,
                year=data.year,
            )
        except Exception as e:
            logger.error(f"[dotacion_gl] GL post failed for payment {payment_id}: {e}")

        return DotacionPaymentResponse(
            success=True,
            data=DotacionPayment(
                id=row["id"],
                tenant_id=row["tenant_id"],
                tenant_member_id=row["tenant_member_id"],
                period=row["period"],
                year=row["year"],
                items_description=row["items_description"],
                total_amount=Decimal(str(row["total_amount"])),
                payment_method=row["payment_method"],
                payment_date=row["payment_date"],
                notes=row["notes"],
                created_at=row["created_at"],
            ),
        )


async def get_dotacion_payments(
    request: Request,
    member_id: UUID,
) -> 'DotacionPaymentListResponse':
    """List dotación payments for an employee, ordered by year DESC, period DESC."""
    from app.models.salary import DotacionPayment, DotacionPaymentListResponse
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM salary_dotacion_payments
               WHERE tenant_member_id = $1 AND tenant_id = $2
               ORDER BY year DESC,
                 CASE period WHEN 'DEC' THEN 3 WHEN 'AUG' THEN 2 ELSE 1 END DESC""",
            member_id, tenant_id,
        )

        payments: List[DotacionPayment] = [
            DotacionPayment(
                id=r["id"],
                tenant_id=r["tenant_id"],
                tenant_member_id=r["tenant_member_id"],
                period=r["period"],
                year=r["year"],
                items_description=r["items_description"],
                total_amount=Decimal(str(r["total_amount"])),
                payment_method=r["payment_method"],
                payment_date=r["payment_date"],
                notes=r["notes"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

        return DotacionPaymentListResponse(success=True, data=payments)


# =============================================================================
# PILA PAYMENTS
# =============================================================================

async def _post_pila_gl_entry(
    conn,
    tenant_id: UUID,
    pila_payment_id: UUID,
    employee_ss_amount: Decimal,
    employer_ss_amount: Decimal,
    total_amount: Decimal,
    payment_method: Optional[str],
    payment_date: Any,
    period_month: str,
) -> None:
    """
    Post PILA disbursement GL entry (3 lines).
    DR 237005 (employee SS) + DR 237010 (employer SS) / CR bank

    Clears accumulated SS liabilities for the given period.
    Graceful degrade — never raises.
    """
    if total_amount <= 0:
        return

    # Idempotency check
    already_posted = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'nomina_pila' AND source_id = $2""",
        tenant_id, pila_payment_id,
    )
    if already_posted:
        logger.info(f"[pila_gl] PILA payment {pila_payment_id} already posted — skip")
        return

    # Resolve entry date
    entry_date = payment_date.date() if hasattr(payment_date, 'date') else (payment_date or date.today())

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry_date.year, entry_date.month,
    )
    if closed:
        logger.warning(
            f"[pila_gl] Period {entry_date.year}-{entry_date.month:02d} closed — skip GL for PILA {pila_payment_id}"
        )
        return

    # Resolve DR accounts: 237005 (employee SS) and 237010 (employer SS)
    dr_237005 = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '237005' AND is_active = true",
        tenant_id,
    )
    dr_237010 = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = '237010' AND is_active = true",
        tenant_id,
    )
    if not dr_237005 or not dr_237010:
        logger.warning(f"[pila_gl] DR accounts 237005/237010 not found for tenant {tenant_id} — skip PILA GL")
        return

    # Resolve CR account: bank/cash
    credit_code = await _resolve_salary_credit_gl_code(conn, payment_method)
    cr_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, credit_code,
    )
    if not cr_acct:
        logger.warning(f"[pila_gl] CR account {credit_code} not found for tenant {tenant_id} — skip PILA GL")
        return

    emp_float = float(employee_ss_amount)
    emp_er_float = float(employer_ss_amount)
    total_float = float(total_amount)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'nomina_pila', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, entry_date.year, entry_date.month,
            f"Pago PILA — {period_month}",
            pila_payment_id, total_float, total_float,
        )
        entry_id = entry_row["id"]

        # DR line 1 — 237005 Aportes SS empleado (clears liability)
        if emp_float > 0:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, 0, $4, 0)""",
                entry_id, dr_237005["id"], emp_float, f"PILA empleado {period_month}",
            )

        # DR line 2 — 237010 Aportes SS empleador (clears liability)
        if emp_er_float > 0:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, 0, $4, 1)""",
                entry_id, dr_237010["id"], emp_er_float, f"PILA empleador {period_month}",
            )

        # CR line — bank/cash
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 2)""",
            entry_id, cr_acct["id"], total_float, f"PILA {period_month}",
        )

    logger.info(
        f"[pila_gl] Posted PILA entry {entry_id} for payment {pila_payment_id} "
        f"(total={total_float}, DR237005={emp_float}, DR237010={emp_er_float}, CR={credit_code}, {period_month})"
    )


async def record_pila_payment(
    request: Request,
    data,
) -> 'PilaPaymentResponse':
    """Register a PILA disbursement — clears 237005 + 237010 liability for a period."""
    from app.models.salary import PilaPayment, PilaPaymentResponse
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        payment_date = data.payment_date or datetime.now()

        row = await conn.fetchrow(
            """INSERT INTO pila_payments
                   (tenant_id, period_month, employee_ss_amount, employer_ss_amount,
                    total_amount, payment_method, payment_date, notes)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING *""",
            tenant_id, data.period_month,
            data.employee_ss_amount, data.employer_ss_amount,
            data.total_amount, data.payment_method, payment_date, data.notes,
        )

        payment_id = row["id"]

        # Post GL entry (graceful degrade)
        try:
            await _post_pila_gl_entry(
                conn=conn,
                tenant_id=tenant_id,
                pila_payment_id=payment_id,
                employee_ss_amount=data.employee_ss_amount,
                employer_ss_amount=data.employer_ss_amount,
                total_amount=data.total_amount,
                payment_method=data.payment_method,
                payment_date=payment_date,
                period_month=data.period_month,
            )
        except Exception as e:
            logger.error(f"[pila_gl] GL post failed for PILA payment {payment_id}: {e}")

        return PilaPaymentResponse(
            success=True,
            data=PilaPayment(
                id=row["id"],
                tenant_id=row["tenant_id"],
                period_month=row["period_month"],
                employee_ss_amount=Decimal(str(row["employee_ss_amount"])),
                employer_ss_amount=Decimal(str(row["employer_ss_amount"])),
                total_amount=Decimal(str(row["total_amount"])),
                payment_method=row["payment_method"],
                payment_date=row["payment_date"],
                notes=row["notes"],
                created_at=row["created_at"],
            ),
        )


async def get_pila_payments(
    request: Request,
) -> 'PilaPaymentListResponse':
    """List all PILA payments for the tenant, ordered by payment_date DESC."""
    from app.models.salary import PilaPayment, PilaPaymentListResponse
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM pila_payments
               WHERE tenant_id = $1
               ORDER BY payment_date DESC, created_at DESC""",
            tenant_id,
        )

        payments: List[PilaPayment] = [
            PilaPayment(
                id=r["id"],
                tenant_id=r["tenant_id"],
                period_month=r["period_month"],
                employee_ss_amount=Decimal(str(r["employee_ss_amount"])),
                employer_ss_amount=Decimal(str(r["employer_ss_amount"])),
                total_amount=Decimal(str(r["total_amount"])),
                payment_method=r["payment_method"],
                payment_date=r["payment_date"],
                notes=r["notes"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

        return PilaPaymentListResponse(success=True, data=payments)


async def get_pila_pending(
    request: Request,
) -> 'PilaPendingResponse':
    """
    Return periods with a net positive SS liability (i.e., PILA not yet fully paid).

    Queries the net credit balance of accounts 237005 and 237010 from tenant_journal_lines,
    grouped by period_year + period_month. Only returns periods where the net liability > 0.
    """
    from app.models.salary import PilaPendingByPeriod, PilaPendingResponse
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                 LPAD(tje.period_year::text, 4, '0') || '-' ||
                 LPAD(tje.period_month::text, 2, '0') AS period_month,
                 ta.code,
                 SUM(tjl.credit) - COALESCE(SUM(tjl.debit), 0) AS net_liability
               FROM tenant_journal_lines tjl
               JOIN tenant_journal_entries tje ON tje.id = tjl.journal_entry_id
               JOIN tenant_accounts ta ON ta.id = tjl.account_id
               WHERE ta.tenant_id = $1
                 AND ta.code IN ('237005', '237010')
               GROUP BY tje.period_year, tje.period_month, ta.code
               HAVING SUM(tjl.credit) - COALESCE(SUM(tjl.debit), 0) > 0
               ORDER BY tje.period_year, tje.period_month, ta.code""",
            tenant_id,
        )

        # Pivot: group by period_month, split by code
        period_map: Dict[str, Dict[str, Decimal]] = {}
        for r in rows:
            pm = r["period_month"]
            code = r["code"]
            net = Decimal(str(r["net_liability"]))
            if pm not in period_map:
                period_map[pm] = {"237005": Decimal("0"), "237010": Decimal("0")}
            period_map[pm][code] = net

        pending: List[PilaPendingByPeriod] = [
            PilaPendingByPeriod(
                period_month=pm,
                employee_ss_pending=vals["237005"],
                employer_ss_pending=vals["237010"],
                total_pending=vals["237005"] + vals["237010"],
            )
            for pm, vals in sorted(period_map.items())
        ]

        return PilaPendingResponse(success=True, data=pending)
