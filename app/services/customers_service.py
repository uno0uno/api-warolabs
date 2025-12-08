"""
Customers Service - Business logic for customer management
"""
from typing import Optional
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import APIError
from app.models.customer import (
    Customer,
    CustomerSearchOrCreate,
    CustomerResponse,
    CustomerSearchResponse
)
import logging

logger = logging.getLogger(__name__)


async def search_or_create_customer(
    request: Request,
    customer_data: CustomerSearchOrCreate
) -> CustomerResponse:
    """
    Search for a customer by phone number, create if doesn't exist.
    Associates customer with the current tenant.

    Args:
        request: FastAPI request object
        customer_data: Customer search/create data

    Returns:
        CustomerResponse with customer data and is_new flag
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.get('tenant_id')

        if not tenant_id:
            raise APIError("No tenant context found", status_code=400)

        # Clean phone number (remove spaces, dashes, etc.)
        phone_number = customer_data.phone_number.strip().replace(' ', '').replace('-', '')

        async with get_db_connection() as conn:
            # Search for existing customer by phone number
            search_query = """
                SELECT
                    id,
                    phone_number,
                    name,
                    email,
                    created_at,
                    updated_at
                FROM profile
                WHERE phone_number = $1
                LIMIT 1
            """

            existing_customer = await conn.fetchrow(search_query, phone_number)

            if existing_customer:
                # Customer found
                logger.info(f"✅ Customer found: {existing_customer['phone_number']}")

                # Check if customer is already associated with tenant
                tenant_association_query = """
                    SELECT tenant_id
                    FROM tenant_members
                    WHERE user_id = $1 AND tenant_id = $2
                """

                association = await conn.fetchrow(
                    tenant_association_query,
                    existing_customer['id'],
                    tenant_id
                )

                # If not associated, create association
                if not association:
                    await conn.execute(
                        """
                        INSERT INTO tenant_members (user_id, tenant_id, role, status)
                        VALUES ($1, $2, 'customer', 'active')
                        ON CONFLICT (user_id, tenant_id) DO NOTHING
                        """,
                        existing_customer['id'],
                        tenant_id
                    )
                    logger.info(f"✅ Associated customer {existing_customer['id']} with tenant {tenant_id}")

                customer = Customer(
                    id=existing_customer['id'],
                    phone_number=existing_customer['phone_number'],
                    name=existing_customer['name'],
                    email=existing_customer['email'],
                    tenant_id=tenant_id,
                    created_at=existing_customer['created_at'],
                    updated_at=existing_customer['updated_at']
                )

                return CustomerResponse(
                    success=True,
                    data=customer,
                    is_new=False
                )

            else:
                # Customer not found, create new one
                logger.info(f"🆕 Creating new customer: {phone_number}")

                # Generate email if not provided
                email = customer_data.email or f"{phone_number}@customer.temp"

                create_query = """
                    INSERT INTO profile (
                        id,
                        phone_number,
                        name,
                        email,
                        nationality_id,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        gen_random_uuid(),
                        $1,
                        $2,
                        $3,
                        170,  -- Default nationality (Colombia)
                        NOW(),
                        NOW()
                    )
                    RETURNING id, phone_number, name, email, created_at, updated_at
                """

                new_customer = await conn.fetchrow(
                    create_query,
                    phone_number,
                    customer_data.name,
                    email
                )

                # Associate customer with tenant
                await conn.execute(
                    """
                    INSERT INTO tenant_members (user_id, tenant_id, role, status)
                    VALUES ($1, $2, 'customer', 'active')
                    """,
                    new_customer['id'],
                    tenant_id
                )

                logger.info(f"✅ Customer created and associated with tenant: {new_customer['id']}")

                customer = Customer(
                    id=new_customer['id'],
                    phone_number=new_customer['phone_number'],
                    name=new_customer['name'],
                    email=new_customer['email'],
                    tenant_id=tenant_id,
                    created_at=new_customer['created_at'],
                    updated_at=new_customer['updated_at']
                )

                return CustomerResponse(
                    success=True,
                    data=customer,
                    is_new=True
                )

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error in search_or_create_customer: {str(e)}")
        raise APIError(f"Error processing customer: {str(e)}", status_code=500)


async def search_customer_by_phone(
    request: Request,
    phone_number: str
) -> CustomerSearchResponse:
    """
    Search for a customer by phone number only.

    Args:
        request: FastAPI request object
        phone_number: Phone number to search

    Returns:
        CustomerSearchResponse with customer data if found
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.get('tenant_id')

        # Clean phone number
        phone_number = phone_number.strip().replace(' ', '').replace('-', '')

        async with get_db_connection() as conn:
            query = """
                SELECT
                    p.id,
                    p.phone_number,
                    p.name,
                    p.email,
                    p.created_at,
                    p.updated_at
                FROM profile p
                WHERE p.phone_number = $1
                LIMIT 1
            """

            result = await conn.fetchrow(query, phone_number)

            if result:
                customer = Customer(
                    id=result['id'],
                    phone_number=result['phone_number'],
                    name=result['name'],
                    email=result['email'],
                    tenant_id=tenant_id,
                    created_at=result['created_at'],
                    updated_at=result['updated_at']
                )

                return CustomerSearchResponse(
                    success=True,
                    customer=customer,
                    found=True
                )
            else:
                return CustomerSearchResponse(
                    success=True,
                    customer=None,
                    found=False
                )

    except Exception as e:
        logger.error(f"Error searching customer: {str(e)}")
        raise APIError(f"Error searching customer: {str(e)}", status_code=500)
