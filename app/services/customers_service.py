"""
Customers Service - Business logic for customer management
"""
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import APIError
from app.models.customer import (
    Customer,
    CustomerSearchOrCreate,
    CustomerResponse,
    CustomerSearchResponse,
    CustomerSummary,
    CustomerQuerySearchResponse,
    CustomerUpdate,
    CustomerUpdateResponse,
    TopProduct,
    CustomerInsights,
    CustomerInsightsResponse
)
from uuid import UUID
import json
import logging

logger = logging.getLogger(__name__)


async def get_customer_by_id(
    request: Request,
    customer_id: UUID,
) -> CustomerUpdateResponse:
    """
    Fetch a customer profile by id, scoped to the current tenant (tenant_members).
    Includes fiscal fields so the POS can reuse invoice data.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise APIError("No tenant context found", status_code=400)

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    p.id,
                    p.phone_number,
                    p.name,
                    p.email,
                    p.fiscal_id_type,
                    p.fiscal_id,
                    p.fiscal_business_name,
                    p.fiscal_email,
                    p.created_at,
                    p.updated_at
                FROM profile p
                JOIN tenant_members tm ON tm.user_id = p.id
                WHERE p.id = $1
                  AND tm.tenant_id = $2
                  AND tm.role = 'customer'
                LIMIT 1
                """,
                customer_id,
                tenant_id,
            )

        if not row:
            raise APIError("Customer not found", status_code=404)

        customer = Customer(
            id=row["id"],
            phone_number=row["phone_number"],
            name=row["name"],
            email=row["email"],
            fiscal_id_type=row["fiscal_id_type"],
            fiscal_id=row["fiscal_id"],
            fiscal_business_name=row["fiscal_business_name"],
            fiscal_email=row["fiscal_email"],
            tenant_id=tenant_id,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

        return CustomerUpdateResponse(success=True, data=customer)

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error in get_customer_by_id: {str(e)}")
        raise APIError(f"Error fetching customer: {str(e)}", status_code=500)


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
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise APIError("No tenant context found", status_code=400)

        # Clean phone number (remove spaces, dashes, etc.)
        phone_number = customer_data.phone_number.strip().replace(' ', '').replace('-', '')

        # Check if phone_number is actually an email
        is_email_input = '@' in phone_number

        async with get_db_connection() as conn:
            # Search for existing customer by phone number or email
            base_select = """
                SELECT
                    id,
                    phone_number,
                    name,
                    email,
                    fiscal_id_type,
                    fiscal_id,
                    fiscal_business_name,
                    fiscal_email,
                    created_at,
                    updated_at
                FROM profile
            """
            if is_email_input:
                search_query = base_select + " WHERE email = $1 LIMIT 1"
            else:
                search_query = base_select + " WHERE phone_number = $1 LIMIT 1"

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
                        INSERT INTO tenant_members (id, user_id, tenant_id, role)
                        VALUES (gen_random_uuid(), $1, $2, 'customer')
                        """,
                        existing_customer['id'],
                        tenant_id
                    )
                    logger.info(f"✅ Associated customer {existing_customer['id']} with tenant {tenant_id}")

                # Backfill fiscal fields if the request brings them and they
                # are not yet set on the profile (or differ from what we have).
                fiscal_payload_present = customer_data.fiscal_id_type and customer_data.fiscal_id
                if fiscal_payload_present:
                    await conn.execute(
                        """
                        UPDATE profile SET
                            fiscal_id_type = $2,
                            fiscal_id = $3,
                            fiscal_business_name = $4,
                            fiscal_email = COALESCE($5, fiscal_email),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        existing_customer['id'],
                        customer_data.fiscal_id_type,
                        customer_data.fiscal_id,
                        customer_data.fiscal_business_name,
                        customer_data.fiscal_email,
                    )
                    refreshed = await conn.fetchrow(
                        base_select + " WHERE id = $1",
                        existing_customer['id'],
                    )
                    existing_customer = refreshed

                customer = Customer(
                    id=existing_customer['id'],
                    phone_number=existing_customer['phone_number'],
                    name=existing_customer['name'],
                    email=existing_customer['email'],
                    fiscal_id_type=existing_customer['fiscal_id_type'],
                    fiscal_id=existing_customer['fiscal_id'],
                    fiscal_business_name=existing_customer['fiscal_business_name'],
                    fiscal_email=existing_customer['fiscal_email'],
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
                # Check if phone_number is actually an email (contains @)
                if '@' in phone_number and not customer_data.email:
                    email = phone_number
                else:
                    email = customer_data.email or f"{phone_number}@customer.temp"

                create_query = """
                    INSERT INTO profile (
                        id,
                        phone_number,
                        name,
                        email,
                        nationality_id,
                        fiscal_id_type,
                        fiscal_id,
                        fiscal_business_name,
                        fiscal_email,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        gen_random_uuid(),
                        $1, $2, $3,
                        170,  -- Default nationality (Colombia)
                        $4, $5, $6, $7,
                        NOW(), NOW()
                    )
                    RETURNING id, phone_number, name, email,
                              fiscal_id_type, fiscal_id, fiscal_business_name, fiscal_email,
                              created_at, updated_at
                """

                new_customer = await conn.fetchrow(
                    create_query,
                    phone_number,
                    customer_data.name,
                    email,
                    customer_data.fiscal_id_type,
                    customer_data.fiscal_id,
                    customer_data.fiscal_business_name,
                    customer_data.fiscal_email,
                )

                # Associate customer with tenant
                await conn.execute(
                    """
                    INSERT INTO tenant_members (id, user_id, tenant_id, role)
                    VALUES (gen_random_uuid(), $1, $2, 'customer')
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
                    fiscal_id_type=new_customer['fiscal_id_type'],
                    fiscal_id=new_customer['fiscal_id'],
                    fiscal_business_name=new_customer['fiscal_business_name'],
                    fiscal_email=new_customer['fiscal_email'],
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
        tenant_id = session_context.tenant_id

        # Clean phone number
        phone_number = phone_number.strip().replace(' ', '').replace('-', '')

        async with get_db_connection() as conn:
            query = """
                SELECT
                    p.id,
                    p.phone_number,
                    p.name,
                    p.email,
                    p.fiscal_id_type,
                    p.fiscal_id,
                    p.fiscal_business_name,
                    p.fiscal_email,
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
                    fiscal_id_type=result['fiscal_id_type'],
                    fiscal_id=result['fiscal_id'],
                    fiscal_business_name=result['fiscal_business_name'],
                    fiscal_email=result['fiscal_email'],
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


async def search_customers_by_query(
    request: Request,
    q: str,
    limit: int = 20
) -> CustomerQuerySearchResponse:
    """
    Search customers by partial name or phone number (ILIKE OR).
    Results are scoped to the current tenant via tenant_members.

    Args:
        request: FastAPI request object
        q: Search query (partial name or phone)
        limit: Maximum number of results (default 20)

    Returns:
        CustomerQuerySearchResponse with list of CustomerSummary
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise APIError("No tenant context found", status_code=400)

        async with get_db_connection() as conn:
            # Match against name, phone, OR fiscal_id (NIT/cédula) so users
            # can find a customer by typing the document number.
            query = """
                SELECT DISTINCT
                    p.id,
                    p.name,
                    p.phone_number,
                    p.email
                FROM profile p
                JOIN tenant_members tm ON tm.user_id = p.id
                WHERE tm.tenant_id = $1
                  AND tm.role = 'customer'
                  AND (
                      p.name ILIKE $2
                      OR p.phone_number ILIKE $2
                      OR p.fiscal_id ILIKE $2
                  )
                ORDER BY p.name
                LIMIT $3
            """

            rows = await conn.fetch(query, tenant_id, f"%{q}%", limit)

            data = [
                CustomerSummary(
                    id=row['id'],
                    name=row['name'],
                    phone_number=row['phone_number'],
                    email=row['email']
                )
                for row in rows
            ]

            return CustomerQuerySearchResponse(success=True, data=data)

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error in search_customers_by_query: {str(e)}")
        raise APIError(f"Error searching customers: {str(e)}", status_code=500)


async def get_customer_insights(
    request: Request,
    customer_id: UUID
) -> CustomerInsightsResponse:
    """
    Return aggregated purchase stats for a customer, scoped to the current tenant.

    Metrics: orders_count, last_order_date, avg_ticket, top_product, avg_days_between_visits.
    All metric fields are None when orders_count == 0.
    Single CTE query — one DB round trip.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise APIError("No tenant context found", status_code=400)

        query = """
            WITH stats AS (
                SELECT
                    COUNT(*)::int                                          AS orders_count,
                    MAX(o.order_date)                                      AS last_order_date,
                    ROUND(AVG(o.total_amount)::numeric, 0)::bigint         AS avg_ticket
                FROM orders o
                WHERE o.customer_id = $1
                  AND o.tenant_id   = $2
                  AND o.status      = 'completed'
            ),
            top_products AS (
                SELECT COALESCE(
                    json_agg(
                        json_build_object('name', sub.pname, 'count', sub.cnt)
                        ORDER BY sub.cnt DESC
                    ),
                    '[]'::json
                ) AS products_json
                FROM (
                    SELECT p.name AS pname, SUM(oi.quantity)::int AS cnt
                    FROM order_items oi
                    JOIN orders  o ON o.id = oi.order_id
                    JOIN product p ON p.id = oi.product_id
                    WHERE o.customer_id = $1
                      AND o.tenant_id   = $2
                      AND o.status      = 'completed'
                    GROUP BY p.id, p.name
                    ORDER BY cnt DESC
                    LIMIT 5
                ) sub
            ),
            freq AS (
                WITH ordered AS (
                    SELECT order_date,
                           LAG(order_date) OVER (ORDER BY order_date) AS prev_date
                    FROM orders
                    WHERE customer_id = $1
                      AND tenant_id   = $2
                      AND status      = 'completed'
                )
                SELECT ROUND(
                    AVG(EXTRACT(EPOCH FROM (order_date - prev_date)) / 86400)::numeric, 1
                ) AS avg_days
                FROM ordered
                WHERE prev_date IS NOT NULL
            )
            SELECT
                s.orders_count,
                s.last_order_date,
                s.avg_ticket,
                tp.products_json,
                f.avg_days
            FROM stats s
            LEFT JOIN top_products tp ON true
            LEFT JOIN freq         f  ON true
        """

        async with get_db_connection() as conn:
            row = await conn.fetchrow(query, customer_id, tenant_id)

        orders_count = row['orders_count'] if row else 0

        if orders_count == 0:
            data = CustomerInsights(orders_count=0)
        else:
            raw_products = row['products_json'] or []
            if isinstance(raw_products, str):
                raw_products = json.loads(raw_products)
            top_products = [
                TopProduct(name=p['name'], count=p['count'])
                for p in (raw_products if isinstance(raw_products, list) else [])
            ] or None
            data = CustomerInsights(
                orders_count=orders_count,
                last_order_date=row['last_order_date'],
                avg_ticket=row['avg_ticket'],
                top_products=top_products,
                avg_days_between_visits=float(row['avg_days']) if row['avg_days'] is not None else None
            )

        return CustomerInsightsResponse(success=True, data=data)

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error in get_customer_insights: {str(e)}")
        raise APIError(f"Error fetching customer insights: {str(e)}", status_code=500)


async def update_customer(
    request: Request,
    customer_id: UUID,
    update_data: CustomerUpdate,
) -> CustomerUpdateResponse:
    """
    Update name, email, and/or phone_number of a customer profile.
    Only fields explicitly provided (non-None) are updated.
    Scoped to the current tenant — only updates customers who are members.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise APIError("No tenant context found", status_code=400)

        # Verify the customer belongs to this tenant
        async with get_db_connection() as conn:
            member_check = await conn.fetchrow(
                """
                SELECT p.id FROM profile p
                JOIN tenant_members tm ON tm.user_id = p.id
                WHERE p.id = $1 AND tm.tenant_id = $2 AND tm.role = 'customer'
                """,
                customer_id, tenant_id
            )
            if not member_check:
                raise APIError("Customer not found", status_code=404)

            # Build SET clause dynamically — only update provided fields
            fields = {}
            if update_data.name is not None:
                fields['name'] = update_data.name.strip() or None
            if update_data.email is not None:
                fields['email'] = update_data.email.strip() or None
            if update_data.phone_number is not None:
                phone = update_data.phone_number.strip().replace(' ', '').replace('-', '')
                fields['phone_number'] = phone
            if update_data.fiscal_id_type is not None:
                fields['fiscal_id_type'] = update_data.fiscal_id_type
            if update_data.fiscal_id is not None:
                fields['fiscal_id'] = update_data.fiscal_id.strip() or None
            if update_data.fiscal_business_name is not None:
                fields['fiscal_business_name'] = update_data.fiscal_business_name.strip() or None
            if update_data.fiscal_email is not None:
                fields['fiscal_email'] = update_data.fiscal_email.strip() or None

            if not fields:
                raise APIError("No fields to update", status_code=400)

            set_parts = [f"{col} = ${i+2}" for i, col in enumerate(fields)]
            values = list(fields.values())
            query = f"""
                UPDATE profile
                SET {', '.join(set_parts)}, updated_at = NOW()
                WHERE id = $1
                RETURNING id, phone_number, name, email,
                          fiscal_id_type, fiscal_id, fiscal_business_name, fiscal_email,
                          created_at, updated_at
            """
            row = await conn.fetchrow(query, customer_id, *values)

        updated = Customer(
            id=row['id'],
            phone_number=row['phone_number'],
            name=row['name'],
            email=row['email'],
            fiscal_id_type=row['fiscal_id_type'],
            fiscal_id=row['fiscal_id'],
            fiscal_business_name=row['fiscal_business_name'],
            fiscal_email=row['fiscal_email'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )
        return CustomerUpdateResponse(success=True, data=updated)

    except APIError:
        raise
    except Exception as e:
        err_str = str(e)
        if 'profile_email_key' in err_str or ('duplicate key' in err_str and 'email' in err_str):
            raise APIError("Este correo ya está registrado en otro perfil", status_code=409)
        if 'profile_phone_number_key' in err_str or ('duplicate key' in err_str and 'phone_number' in err_str):
            raise APIError("Este número de teléfono ya está registrado en otro perfil", status_code=409)
        logger.error(f"Error in update_customer: {err_str}")
        raise APIError(f"Error al actualizar el cliente", status_code=500)
