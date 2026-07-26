import logging
from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import time
import asyncpg
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.services.billing_service import check_plan_quota_growth
from app.models.supplier import (
    Supplier,
    SupplierCreate,
    SupplierUpdate,
    SupplierResponse,
    SuppliersListResponse,
    SupplierStats
)

logger = logging.getLogger(__name__)

async def _get_suppliers_for_tenant(
    tenant_id: str,
    *,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    is_active: Optional[bool] = None,
    payment_terms: Optional[str] = None
) -> SuppliersListResponse:
    """
    Get suppliers list with tenant isolation following database governance
    """
    try:
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Build query with tenant isolation
            base_query = """
                SELECT
                    id,
                    tenant_id,
                    name,
                    description,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active,
                    access_token,
                    created_at,
                    updated_at
                FROM tenant_suppliers
                WHERE tenant_id = $1
            """
            
            count_query = """
                SELECT COUNT(*) as total
                FROM tenant_suppliers 
                WHERE tenant_id = $1
            """
            
            params = [tenant_id]
            param_count = 2
            
            # Add filters
            if search:
                if search_field and search_field in ['name', 'tax_id', 'email', 'phone']:
                    # Search in specific field
                    base_query += f" AND LOWER({search_field}) LIKE LOWER(${param_count})"
                    count_query += f" AND LOWER({search_field}) LIKE LOWER(${param_count})"
                    params.append(f"%{search}%")
                    param_count += 1
                else:
                    # Default search in name or tax_id
                    base_query += f" AND (LOWER(name) LIKE LOWER(${param_count}) OR LOWER(tax_id) LIKE LOWER(${param_count}))"
                    count_query += f" AND (LOWER(name) LIKE LOWER(${param_count}) OR LOWER(tax_id) LIKE LOWER(${param_count}))"
                    params.append(f"%{search}%")
                    param_count += 1
            
            if is_active is not None:
                base_query += f" AND is_active = ${param_count}"
                count_query += f" AND is_active = ${param_count}"
                params.append(is_active)
                param_count += 1
            
            if payment_terms:
                base_query += f" AND LOWER(payment_terms) = LOWER(${param_count})"
                count_query += f" AND LOWER(payment_terms) = LOWER(${param_count})"
                params.append(payment_terms)
                param_count += 1
            
            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])
            
            # Execute queries
            suppliers_data = await conn.fetch(base_query, *params)
            count_result = await conn.fetchrow(count_query, *params[:-2])  # Exclude limit and offset

            # Calculate stats from ALL suppliers (not just current page)
            stats_query = """
                SELECT
                    COUNT(*) FILTER (WHERE is_active = true) as activos,
                    COUNT(*) FILTER (WHERE is_active = false) as inactivos,
                    COALESCE(
                        ROUND(
                            AVG(
                                CASE
                                    WHEN payment_terms ~ '^[0-9]+'
                                    THEN CAST(SUBSTRING(payment_terms FROM '^[0-9]+') AS INTEGER)
                                    ELSE NULL
                                END
                            )
                        ),
                        0
                    ) as promedio_pago
                FROM tenant_suppliers
                WHERE tenant_id = $1
            """
            stats_result = await conn.fetchrow(stats_query, tenant_id)

            # Convert to models
            suppliers = []
            for row in suppliers_data:
                supplier = Supplier(
                    id=row['id'],
                    tenantId=row['tenant_id'],
                    name=row['name'],
                    description=row['description'],
                    contact_info=row['contact_info'],
                    tax_id=row['tax_id'],
                    address=row['address'],
                    phone=row['phone'],
                    email=row['email'],
                    payment_terms=row['payment_terms'],
                    is_active=row['is_active'],
                    access_token=row['access_token'],
                    createdAt=row['created_at'],
                    updatedAt=row['updated_at']
                )
                suppliers.append(supplier)

            # Create stats object
            stats = SupplierStats(
                activos=int(stats_result['activos'] or 0),
                inactivos=int(stats_result['inactivos'] or 0),
                promedio_pago=int(stats_result['promedio_pago'] or 0),
                con_entregas=0  # TODO: Implement delivery tracking
            )

            response_data = SuppliersListResponse(
                data=suppliers,
                total=count_result['total'],
                page=page,
                limit=limit,
                stats=stats
            )

            return response_data

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching suppliers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_suppliers_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    is_active: Optional[bool] = None,
    payment_terms: Optional[str] = None
) -> SuppliersListResponse:
    """
    Get suppliers list for the current session tenant.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    return await _get_suppliers_for_tenant(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        search_field=search_field,
        is_active=is_active,
        payment_terms=payment_terms
    )

async def get_supplier_by_id(
    request: Request,
    response: Response,
    supplier_id: UUID
) -> SupplierResponse:
    """
    Get a specific supplier by ID with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            supplier_data = await conn.fetchrow("""
                SELECT
                    id,
                    tenant_id,
                    name,
                    description,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active,
                    access_token,
                    created_at,
                    updated_at
                FROM tenant_suppliers
                WHERE id = $1 AND tenant_id = $2
            """, supplier_id, tenant_id)

            if not supplier_data:
                raise HTTPException(status_code=404, detail="Supplier not found")

            supplier = Supplier(
                id=supplier_data['id'],
                tenantId=supplier_data['tenant_id'],
                name=supplier_data['name'],
                description=supplier_data['description'],
                contact_info=supplier_data['contact_info'],
                tax_id=supplier_data['tax_id'],
                address=supplier_data['address'],
                phone=supplier_data['phone'],
                email=supplier_data['email'],
                payment_terms=supplier_data['payment_terms'],
                is_active=supplier_data['is_active'],
                access_token=supplier_data['access_token'],
                createdAt=supplier_data['created_at'],
                updatedAt=supplier_data['updated_at']
            )

            return SupplierResponse(data=supplier)
            
    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching supplier {supplier_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_supplier(
    request: Request,
    response: Response,
    supplier_data: SupplierCreate
) -> SupplierResponse:
    """
    Create a new supplier with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Create supplier and payment agreements in transaction
        async with get_db_connection() as conn:
            await check_plan_quota_growth(conn, tenant_id, "tenant_suppliers")
            # Insert new supplier
            new_supplier = await conn.fetchrow("""
                INSERT INTO tenant_suppliers (
                    tenant_id,
                    name,
                    description,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING
                    id,
                    tenant_id,
                    name,
                    description,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active,
                    created_at,
                    updated_at
            """,
                tenant_id,
                supplier_data.name,
                supplier_data.description,
                supplier_data.contact_info,
                supplier_data.tax_id,
                supplier_data.address,
                supplier_data.phone,
                supplier_data.email,
                supplier_data.payment_terms,
                supplier_data.is_active
            )

            supplier = Supplier(
                id=new_supplier['id'],
                tenantId=new_supplier['tenant_id'],
                name=new_supplier['name'],
                description=new_supplier['description'],
                contact_info=new_supplier['contact_info'],
                tax_id=new_supplier['tax_id'],
                address=new_supplier['address'],
                phone=new_supplier['phone'],
                email=new_supplier['email'],
                payment_terms=new_supplier['payment_terms'],
                is_active=new_supplier['is_active'],
                createdAt=new_supplier['created_at'],
                updatedAt=new_supplier['updated_at']
            )

            # Create payment agreements if provided
            if supplier_data.payment_agreements:
                logger.info(f"Creating {len(supplier_data.payment_agreements)} payment agreements for supplier {supplier.id}")
                user_id = session_context.user_id

                for agreement_data in supplier_data.payment_agreements:
                    try:
                        # Convert payment_hour string to time object if provided
                        payment_hour = agreement_data.get('payment_hour')
                        if payment_hour and isinstance(payment_hour, str):
                            # Parse time string (format: "HH:MM:SS" or "HH:MM")
                            parts = payment_hour.split(':')
                            payment_hour = time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
                        elif not payment_hour:
                            payment_hour = time(23, 59, 0)  # Default to 23:59:00

                        await conn.execute(
                            """
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
                            """,
                            tenant_id,
                            supplier.id,
                            agreement_data.get('name'),
                            agreement_data.get('description'),
                            agreement_data.get('agreement_type'),
                            agreement_data.get('days_offset'),
                            agreement_data.get('specific_day'),
                            payment_hour,
                            agreement_data.get('valid_from'),
                            agreement_data.get('valid_until'),
                            agreement_data.get('auto_apply', False),
                            agreement_data.get('is_active', True),
                            agreement_data.get('priority', 0),
                            agreement_data.get('metadata'),
                            user_id
                        )
                        logger.info(f"Created payment agreement '{agreement_data.get('name')}' for supplier {supplier.id}")
                    except Exception as e:
                        logger.error(f"Error creating payment agreement: {str(e)}", exc_info=True)
                        # Continue with other agreements even if one fails

        # Send Discord notification outside transaction (non-blocking)
        try:
            from app.services.discord_service import discord_service

            if discord_service:
                # Get tenant and user info for notification using separate connection
                async with get_db_connection(use_transaction=False) as conn:
                    tenant_data = await conn.fetchrow("SELECT name FROM tenants WHERE id = $1", tenant_id)
                    user_data = await conn.fetchrow("SELECT name, user_name FROM profile WHERE id = $1", session_context.user_id)

                    tenant_name = tenant_data['name'] if tenant_data else None
                    user_name = user_data['name'] or user_data['user_name'] if user_data else None

                # Send notification asynchronously
                await discord_service.notify_new_supplier(
                    supplier_name=supplier.name,
                    supplier_email=supplier.email,
                    supplier_phone=supplier.phone,
                    tax_id=supplier.tax_id,
                    payment_terms=supplier.payment_terms,
                    tenant_name=tenant_name,
                    user_name=user_name
                )
        except Exception as notify_error:
            # Log error but don't fail the supplier creation
            logger.warning(f"Failed to send Discord notification: {notify_error}")

        return SupplierResponse(data=supplier)
            
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error creating supplier: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def update_supplier(
    request: Request,
    response: Response,
    supplier_id: UUID,
    supplier_data: SupplierUpdate
) -> SupplierResponse:
    """
    Update an existing supplier with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify supplier exists and belongs to tenant
            existing_supplier = await conn.fetchrow("""
                SELECT id FROM tenant_suppliers
                WHERE id = $1 AND tenant_id = $2
            """, supplier_id, tenant_id)

            if not existing_supplier:
                raise HTTPException(status_code=404, detail="Supplier not found")
            
            # Build update query dynamically
            update_fields = []
            params = [supplier_id, tenant_id]
            param_count = 3
            
            for field, value in supplier_data.dict(exclude_unset=True).items():
                update_fields.append(f"{field} = ${param_count}")
                params.append(value)
                param_count += 1
            
            if not update_fields:
                raise HTTPException(status_code=400, detail="No fields to update")
            
            # Add updated_at
            update_fields.append(f"updated_at = NOW()")
            
            update_query = f"""
                UPDATE tenant_suppliers
                SET {', '.join(update_fields)}
                WHERE id = $1 AND tenant_id = $2
                RETURNING
                    id,
                    tenant_id,
                    name,
                    description,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active,
                    created_at,
                    updated_at
            """
            
            updated_supplier = await conn.fetchrow(update_query, *params)

            supplier = Supplier(
                id=updated_supplier['id'],
                tenantId=updated_supplier['tenant_id'],
                name=updated_supplier['name'],
                description=updated_supplier['description'],
                contact_info=updated_supplier['contact_info'],
                tax_id=updated_supplier['tax_id'],
                address=updated_supplier['address'],
                phone=updated_supplier['phone'],
                email=updated_supplier['email'],
                payment_terms=updated_supplier['payment_terms'],
                is_active=updated_supplier['is_active'],
                createdAt=updated_supplier['created_at'],
                updatedAt=updated_supplier['updated_at']
            )
            
            return SupplierResponse(data=supplier)
            
    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating supplier {supplier_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def delete_supplier(
    request: Request,
    response: Response,
    supplier_id: UUID
) -> Dict[str, Any]:
    """
    Delete a supplier with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify supplier exists and belongs to tenant
            existing_supplier = await conn.fetchrow("""
                SELECT id FROM tenant_suppliers
                WHERE id = $1 AND tenant_id = $2
            """, supplier_id, tenant_id)

            if not existing_supplier:
                raise HTTPException(status_code=404, detail="Supplier not found")

            # Pre-count RESTRICT dependents so the client gets an actionable
            # 409 instead of a generic 500 from a deferred FK violation.
            # supplier_payment_agreements is ON DELETE CASCADE — not counted.
            deps = await conn.fetchrow(
                """
                SELECT
                    COALESCE((SELECT COUNT(*) FROM tenant_purchases       WHERE supplier_id = $1), 0) AS purchases,
                    COALESCE((SELECT COUNT(*) FROM tenant_supplier_prices WHERE supplier_id = $1), 0) AS supplier_prices
                """,
                supplier_id,
            )
            if deps["purchases"] > 0 or deps["supplier_prices"] > 0:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "supplier_has_dependents",
                        "counts": {
                            "purchases": deps["purchases"],
                            "supplier_prices": deps["supplier_prices"],
                        },
                        "message": "El proveedor tiene registros asociados que impiden su eliminación.",
                    },
                )

            try:
                await conn.execute("""
                    DELETE FROM tenant_suppliers
                    WHERE id = $1 AND tenant_id = $2
                """, supplier_id, tenant_id)
            except asyncpg.exceptions.ForeignKeyViolationError as fk_err:
                # Defense-in-depth: a future FK without ON DELETE CASCADE would
                # land here instead of leaking a generic 500. Pre-count above
                # covers the known cases; this protects against schema drift.
                logger.warning(
                    "FK violation deleting supplier %s after pre-count passed: %s",
                    supplier_id,
                    fk_err,
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "supplier_has_dependents_unknown",
                        "message": "El proveedor tiene registros asociados que impiden su eliminación.",
                    },
                )

            return {
                "success": True,
                "message": "Supplier deleted successfully"
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting supplier {supplier_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")
