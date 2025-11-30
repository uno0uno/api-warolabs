import logging
from typing import Optional, List
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.payment_agreement import (
    PaymentAgreement,
    PaymentAgreementCreate,
    PaymentAgreementUpdate,
    PaymentAgreementResponse,
    PaymentAgreementsListResponse
)

logger = logging.getLogger(__name__)

async def get_payment_agreements_list(
    request: Request,
    response: Response,
    supplier_id: UUID
) -> PaymentAgreementsListResponse:
    """
    Get payment agreements for a supplier with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify supplier belongs to tenant
            supplier_check = await conn.fetchrow(
                "SELECT id FROM tenant_suppliers WHERE id = $1 AND tenant_id = $2",
                supplier_id, tenant_id
            )

            if not supplier_check:
                raise HTTPException(status_code=404, detail="Supplier not found")

            # Get payment agreements
            query = """
                SELECT
                    id,
                    tenant_id,
                    supplier_id,
                    name,
                    description,
                    agreement_type,
                    days_offset,
                    specific_day,
                    payment_hour,
                    valid_from,
                    valid_until,
                    auto_apply,
                    is_active,
                    priority,
                    metadata,
                    created_at,
                    updated_at,
                    created_by
                FROM supplier_payment_agreements
                WHERE supplier_id = $1 AND tenant_id = $2
                ORDER BY priority DESC, created_at DESC
            """

            agreements_data = await conn.fetch(query, supplier_id, tenant_id)

            agreements = [
                PaymentAgreement(
                    id=row['id'],
                    tenantId=row['tenant_id'],
                    supplierId=row['supplier_id'],
                    name=row['name'],
                    description=row['description'],
                    agreement_type=row['agreement_type'],
                    days_offset=row['days_offset'],
                    specific_day=row['specific_day'],
                    payment_hour=row['payment_hour'],
                    valid_from=row['valid_from'],
                    valid_until=row['valid_until'],
                    auto_apply=row['auto_apply'],
                    is_active=row['is_active'],
                    priority=row['priority'],
                    metadata=row['metadata'],
                    createdAt=row['created_at'],
                    updatedAt=row['updated_at'],
                    createdBy=row['created_by']
                )
                for row in agreements_data
            ]

            return PaymentAgreementsListResponse(
                success=True,
                data=agreements,
                total=len(agreements)
            )

    except AuthenticationError as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payment agreements: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_payment_agreement_by_id(
    request: Request,
    response: Response,
    supplier_id: UUID,
    agreement_id: UUID
) -> PaymentAgreementResponse:
    """
    Get a specific payment agreement by ID with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            query = """
                SELECT
                    id,
                    tenant_id,
                    supplier_id,
                    name,
                    description,
                    agreement_type,
                    days_offset,
                    specific_day,
                    payment_hour,
                    valid_from,
                    valid_until,
                    auto_apply,
                    is_active,
                    priority,
                    metadata,
                    created_at,
                    updated_at,
                    created_by
                FROM supplier_payment_agreements
                WHERE id = $1 AND supplier_id = $2 AND tenant_id = $3
            """

            row = await conn.fetchrow(query, agreement_id, supplier_id, tenant_id)

            if not row:
                raise HTTPException(status_code=404, detail="Payment agreement not found")

            agreement = PaymentAgreement(
                id=row['id'],
                tenantId=row['tenant_id'],
                supplierId=row['supplier_id'],
                name=row['name'],
                description=row['description'],
                agreement_type=row['agreement_type'],
                days_offset=row['days_offset'],
                specific_day=row['specific_day'],
                payment_hour=row['payment_hour'],
                valid_from=row['valid_from'],
                valid_until=row['valid_until'],
                auto_apply=row['auto_apply'],
                is_active=row['is_active'],
                priority=row['priority'],
                metadata=row['metadata'],
                createdAt=row['created_at'],
                updatedAt=row['updated_at'],
                createdBy=row['created_by']
            )

            return PaymentAgreementResponse(success=True, data=agreement)

    except AuthenticationError as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payment agreement: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


async def create_payment_agreement(
    request: Request,
    response: Response,
    supplier_id: UUID,
    agreement_data: PaymentAgreementCreate
) -> PaymentAgreementResponse:
    """
    Create a new payment agreement with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify supplier belongs to tenant
            supplier_check = await conn.fetchrow(
                "SELECT id FROM tenant_suppliers WHERE id = $1 AND tenant_id = $2",
                supplier_id, tenant_id
            )

            if not supplier_check:
                raise HTTPException(status_code=404, detail="Supplier not found")

            # Create agreement
            query = """
                INSERT INTO supplier_payment_agreements (
                    tenant_id,
                    supplier_id,
                    name,
                    description,
                    agreement_type,
                    days_offset,
                    specific_day,
                    payment_hour,
                    valid_from,
                    valid_until,
                    auto_apply,
                    is_active,
                    priority,
                    metadata,
                    created_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING
                    id,
                    tenant_id,
                    supplier_id,
                    name,
                    description,
                    agreement_type,
                    days_offset,
                    specific_day,
                    payment_hour,
                    valid_from,
                    valid_until,
                    auto_apply,
                    is_active,
                    priority,
                    metadata,
                    created_at,
                    updated_at,
                    created_by
            """

            row = await conn.fetchrow(
                query,
                tenant_id,
                supplier_id,
                agreement_data.name,
                agreement_data.description,
                agreement_data.agreement_type,
                agreement_data.days_offset,
                agreement_data.specific_day,
                agreement_data.payment_hour,
                agreement_data.valid_from,
                agreement_data.valid_until,
                agreement_data.auto_apply,
                agreement_data.is_active,
                agreement_data.priority,
                agreement_data.metadata,
                user_id
            )

            agreement = PaymentAgreement(
                id=row['id'],
                tenantId=row['tenant_id'],
                supplierId=row['supplier_id'],
                name=row['name'],
                description=row['description'],
                agreement_type=row['agreement_type'],
                days_offset=row['days_offset'],
                specific_day=row['specific_day'],
                payment_hour=row['payment_hour'],
                valid_from=row['valid_from'],
                valid_until=row['valid_until'],
                auto_apply=row['auto_apply'],
                is_active=row['is_active'],
                priority=row['priority'],
                metadata=row['metadata'],
                createdAt=row['created_at'],
                updatedAt=row['updated_at'],
                createdBy=row['created_by']
            )

            logger.info(f"Created payment agreement {agreement.id} for supplier {supplier_id}")

            return PaymentAgreementResponse(success=True, data=agreement)

    except AuthenticationError as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating payment agreement: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


async def update_payment_agreement(
    request: Request,
    response: Response,
    supplier_id: UUID,
    agreement_id: UUID,
    agreement_data: PaymentAgreementUpdate
) -> PaymentAgreementResponse:
    """
    Update an existing payment agreement with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Check if agreement exists and belongs to tenant
            existing = await conn.fetchrow(
                """
                SELECT id FROM supplier_payment_agreements
                WHERE id = $1 AND supplier_id = $2 AND tenant_id = $3
                """,
                agreement_id, supplier_id, tenant_id
            )

            if not existing:
                raise HTTPException(status_code=404, detail="Payment agreement not found")

            # Build update query dynamically
            update_fields = []
            params = []
            param_count = 1

            for field, value in agreement_data.model_dump(exclude_unset=True).items():
                update_fields.append(f"{field} = ${param_count}")
                params.append(value)
                param_count += 1

            if not update_fields:
                # No fields to update, return current agreement
                return await get_payment_agreement_by_id(request, response, supplier_id, agreement_id)

            params.extend([agreement_id, supplier_id, tenant_id])

            query = f"""
                UPDATE supplier_payment_agreements
                SET {', '.join(update_fields)}
                WHERE id = ${param_count} AND supplier_id = ${param_count + 1} AND tenant_id = ${param_count + 2}
                RETURNING
                    id,
                    tenant_id,
                    supplier_id,
                    name,
                    description,
                    agreement_type,
                    days_offset,
                    specific_day,
                    payment_hour,
                    valid_from,
                    valid_until,
                    auto_apply,
                    is_active,
                    priority,
                    metadata,
                    created_at,
                    updated_at,
                    created_by
            """

            row = await conn.fetchrow(query, *params)

            agreement = PaymentAgreement(
                id=row['id'],
                tenantId=row['tenant_id'],
                supplierId=row['supplier_id'],
                name=row['name'],
                description=row['description'],
                agreement_type=row['agreement_type'],
                days_offset=row['days_offset'],
                specific_day=row['specific_day'],
                payment_hour=row['payment_hour'],
                valid_from=row['valid_from'],
                valid_until=row['valid_until'],
                auto_apply=row['auto_apply'],
                is_active=row['is_active'],
                priority=row['priority'],
                metadata=row['metadata'],
                createdAt=row['created_at'],
                updatedAt=row['updated_at'],
                createdBy=row['created_by']
            )

            logger.info(f"Updated payment agreement {agreement_id}")

            return PaymentAgreementResponse(success=True, data=agreement)

    except AuthenticationError as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating payment agreement: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


async def delete_payment_agreement(
    request: Request,
    response: Response,
    supplier_id: UUID,
    agreement_id: UUID
):
    """
    Delete a payment agreement with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            result = await conn.execute(
                """
                DELETE FROM supplier_payment_agreements
                WHERE id = $1 AND supplier_id = $2 AND tenant_id = $3
                """,
                agreement_id, supplier_id, tenant_id
            )

            if result == "DELETE 0":
                raise HTTPException(status_code=404, detail="Payment agreement not found")

            logger.info(f"Deleted payment agreement {agreement_id}")

            return {"success": True, "message": "Payment agreement deleted successfully"}

    except AuthenticationError as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting payment agreement: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
