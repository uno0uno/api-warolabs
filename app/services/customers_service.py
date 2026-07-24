"""
Customers Service - Business logic for customer management
"""
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import APIError
from app.core.email_utils import normalize_email
from app.services.customer_relationship_service import upsert_tenant_customer
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
    CustomerInsightsResponse,
    normalize_fiscal_id,
)
from typing import Optional
from uuid import UUID
import json
import logging
import unicodedata

logger = logging.getLogger(__name__)

ANONYMOUS_PHONE = "0000000000"
GENERIC_CUSTOMER_EMAIL = "generico@warocol.com"


def _fold_ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.strip().lower())
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


def rank_anonymous_phone_profile(name: Optional[str], email: Optional[str]) -> int:
    """Lower rank wins. Prefer shared Genérico over other profiles with phone 0000000000."""
    email_l = (email or "").strip().lower()
    if email_l == GENERIC_CUSTOMER_EMAIL:
        return 0
    if _fold_ascii(name or "") == "generico":
        return 1
    return 2


_PROFILE_PHONE_ORDER_SQL = """
ORDER BY
  CASE
    WHEN lower(trim(coalesce(email, ''))) = 'generico@warocol.com' THEN 0
    WHEN lower(trim(coalesce(name, ''))) IN ('genérico', 'generico') THEN 1
    ELSE 2
  END,
  created_at ASC NULLS LAST
"""


async def get_customer_by_id(
    request: Request,
    customer_id: UUID,
) -> CustomerUpdateResponse:
    """
    Fetch a customer profile by id, scoped to the current tenant customer relationship.
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
                JOIN tenant_customers tc ON tc.profile_id = p.id
                WHERE p.id = $1
                  AND tc.tenant_id = $2
                  AND tc.is_active = true
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
        if is_email_input:
            phone_number = normalize_email(phone_number)

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
                search_query = base_select + " WHERE lower(trim(email)) = $1 LIMIT 1"
            elif phone_number == ANONYMOUS_PHONE:
                search_query = (
                    base_select
                    + " WHERE phone_number = $1 "
                    + _PROFILE_PHONE_ORDER_SQL
                    + " LIMIT 1"
                )
            else:
                search_query = base_select + " WHERE phone_number = $1 LIMIT 1"

            existing_customer = await conn.fetchrow(search_query, phone_number)

            if existing_customer:
                # Customer found
                logger.info(f"✅ Customer found: {existing_customer['phone_number']}")

                await upsert_tenant_customer(conn, existing_customer['id'], tenant_id)
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
                        normalize_fiscal_id(customer_data.fiscal_id),
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
                    normalize_fiscal_id(customer_data.fiscal_id),
                    customer_data.fiscal_business_name,
                    customer_data.fiscal_email,
                )

                await upsert_tenant_customer(conn, new_customer['id'], tenant_id)

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
            if phone_number == ANONYMOUS_PHONE:
                query = f"""
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
                {_PROFILE_PHONE_ORDER_SQL}
                LIMIT 1
                """
            else:
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
    Search customers by name, phone number, fiscal ID, or business name.
    Results are scoped to the current tenant via tenant_customers.

    Args:
        request: FastAPI request object
        q: Search query (exact, prefix, or partial match)
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
            # Rank exact matches before prefixes and partial matches so the
            # most relevant customers are not displaced by the result limit.
            query = """
                WITH matching_customers AS (
                    SELECT DISTINCT
                        p.id,
                        p.name,
                        p.phone_number,
                        p.email,
                        p.fiscal_id,
                        p.fiscal_id_type,
                        p.fiscal_business_name,
                        CASE
                            WHEN (
                                p.name ILIKE $2
                                OR p.phone_number ILIKE $2
                                OR p.fiscal_id ILIKE $2
                                OR p.fiscal_business_name ILIKE $2
                            ) THEN 0
                            WHEN (
                                p.name ILIKE $3
                                OR p.phone_number ILIKE $3
                                OR p.fiscal_id ILIKE $3
                                OR p.fiscal_business_name ILIKE $3
                            ) THEN 1
                            ELSE 2
                        END AS match_rank
                    FROM profile p
                    JOIN tenant_customers tc ON tc.profile_id = p.id
                    WHERE tc.tenant_id = $1
                      AND tc.is_active = true
                      AND (
                          p.name ILIKE $4
                          OR p.phone_number ILIKE $4
                          OR p.fiscal_id ILIKE $4
                          OR p.fiscal_business_name ILIKE $4
                      )
                )
                SELECT
                    id,
                    name,
                    phone_number,
                    email,
                    fiscal_id,
                    fiscal_id_type,
                    fiscal_business_name
                FROM matching_customers
                ORDER BY match_rank, name NULLS LAST, id
                LIMIT $5
            """

            rows = await conn.fetch(
                query,
                tenant_id,
                q,
                f"{q}%",
                f"%{q}%",
                limit,
            )

            data = [
                CustomerSummary(
                    id=row['id'],
                    name=row['name'],
                    phone_number=row['phone_number'],
                    email=row['email'],
                    fiscal_id=row['fiscal_id'],
                    fiscal_id_type=row['fiscal_id_type'],
                    fiscal_business_name=row['fiscal_business_name'],
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
    Scoped to the current tenant — only updates profiles with a customer relationship.
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
                JOIN tenant_customers tc ON tc.profile_id = p.id
                WHERE p.id = $1
                  AND tc.tenant_id = $2
                  AND tc.is_active = true
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
                fields['fiscal_id'] = normalize_fiscal_id(update_data.fiscal_id)
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
