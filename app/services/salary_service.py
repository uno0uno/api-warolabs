"""
Salary Service
Handles employee salary configuration and payment management
"""
import logging
from fastapi import Request, UploadFile
from typing import List, Optional
from uuid import UUID, uuid4
from decimal import Decimal
from datetime import datetime
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import ValidationError, NotFoundError, AuthorizationError
from app.services.aws_s3_service import AWSS3Service
from app.models.salary import (
    EmployeesWithSalaryResponse,
    EmployeeDetailResponse,
    SalaryConfigResponse,
    SalaryPaymentResponse,
    SalaryPaymentsListResponse,
    EmployeeSalaryConfigCreate,
    EmployeeWithSalary,
    EmployeeDetailWithPayments,
    EmployeeSalaryConfig,
    SalaryPayment,
    SalaryAttachment
)

logger = logging.getLogger(__name__)

# SMMLV 2026 - Should be fetched from database
DEFAULT_SMMLV = Decimal('1423500')


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
                es.notes as salary_notes
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

        # Calculate base and total salary
        if config.salary_type == 'smmlv':
            if not config.minimum_wage_multiplier:
                raise ValidationError("minimum_wage_multiplier is required for SMMLV salary type")
            base_salary = config.minimum_wage_multiplier * smmlv
            total_salary = base_salary
        elif config.salary_type == 'hourly':
            if not config.hourly_rate:
                raise ValidationError("hourly_rate is required for hourly salary type")
            base_salary = config.hourly_rate
            total_salary = base_salary
        else:  # fixed
            if not config.fixed_amount:
                raise ValidationError("fixed_amount is required for fixed salary type")
            base_salary = config.fixed_amount
            total_salary = base_salary

        # Upsert salary config
        upsert_query = """
            INSERT INTO employee_salaries (
                id, tenant_member_id, period_month, salary_type,
                minimum_wage_multiplier, fixed_amount, hourly_rate, base_salary,
                total_salary, notes, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (tenant_member_id, period_month)
            DO UPDATE SET
                salary_type = EXCLUDED.salary_type,
                minimum_wage_multiplier = EXCLUDED.minimum_wage_multiplier,
                fixed_amount = EXCLUDED.fixed_amount,
                hourly_rate = EXCLUDED.hourly_rate,
                base_salary = EXCLUDED.base_salary,
                total_salary = EXCLUDED.total_salary,
                notes = EXCLUDED.notes
            RETURNING id, tenant_member_id, period_month, salary_type,
                      minimum_wage_multiplier, fixed_amount, hourly_rate, base_salary,
                      total_salary, notes, created_at
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
            config.notes
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
            created_at=row['created_at']
        )

        logger.info(f"Salary configured for employee {employee_id}: {config.salary_type}")

        return SalaryConfigResponse(success=True, data=salary_config)


async def record_salary_payment(
    request: Request,
    tenant_member_id: UUID,
    payment_amount: Decimal,
    payment_method: str,
    payment_date: datetime,
    period_month: str,
    payment_reference: Optional[str] = None,
    notes: Optional[str] = None,
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

        # Create payment
        payment_id = uuid4()
        insert_query = """
            INSERT INTO salary_payments (
                id, tenant_id, tenant_member_id, period_month,
                payment_amount, payment_method, payment_reference,
                payment_date, notes, created_by, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())
            RETURNING id, tenant_id, tenant_member_id, period_month,
                      payment_amount, payment_method, payment_reference,
                      payment_date, notes, created_by, created_at, updated_at
        """

        row = await conn.fetchrow(
            insert_query,
            payment_id,
            tenant_id,
            tenant_member_id,
            period_month,
            payment_amount,
            payment_method,
            payment_reference,
            payment_date,
            notes,
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
            created_by=row['created_by'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            attachments=uploaded_attachments
        )

        logger.info(f"Salary payment recorded for employee {tenant_member_id}: {payment_amount}")

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


async def get_payment_detail(request: Request, payment_id: UUID) -> SalaryPaymentResponse:
    """Get payment detail with attachments"""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM salary_payments
               WHERE id = $1 AND tenant_id = $2""",
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
            created_by=row['created_by'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            attachments=attachments
        )

        return SalaryPaymentResponse(success=True, data=payment)


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

        logger.info(f"Salary payment {payment_id} deleted")

        return {"success": True, "message": "Payment deleted successfully"}
