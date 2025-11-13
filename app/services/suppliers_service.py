import logging
from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.supplier import (
    Supplier,
    SupplierCreate,
    SupplierUpdate,
    SupplierResponse,
    SuppliersListResponse
)

logger = logging.getLogger(__name__)

async def get_suppliers_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    payment_terms: Optional[str] = None
) -> SuppliersListResponse:
    """
    Get suppliers list with tenant isolation following database governance
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build query with tenant isolation
            base_query = """
                SELECT 
                    id,
                    tenant_id,
                    name,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active,
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

            # Convert to models
            suppliers = []
            for row in suppliers_data:
                supplier = Supplier(
                    id=row['id'],
                    tenantId=row['tenant_id'],
                    name=row['name'],
                    contact_info=row['contact_info'],
                    tax_id=row['tax_id'],
                    address=row['address'],
                    phone=row['phone'],
                    email=row['email'],
                    payment_terms=row['payment_terms'],
                    is_active=row['is_active'],
                    createdAt=row['created_at'],
                    updatedAt=row['updated_at']
                )
                suppliers.append(supplier)

            response_data = SuppliersListResponse(
                data=suppliers,
                total=count_result['total'],
                page=page,
                limit=limit
            )

            return response_data

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching suppliers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

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
        
        async with get_db_connection() as conn:
            supplier_data = await conn.fetchrow("""
                SELECT 
                    id,
                    tenant_id,
                    name,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active,
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
                contact_info=supplier_data['contact_info'],
                tax_id=supplier_data['tax_id'],
                address=supplier_data['address'],
                phone=supplier_data['phone'],
                email=supplier_data['email'],
                payment_terms=supplier_data['payment_terms'],
                is_active=supplier_data['is_active'],
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

        async with get_db_connection() as conn:
            # Insert new supplier
            new_supplier = await conn.fetchrow("""
                INSERT INTO tenant_suppliers (
                    tenant_id,
                    name,
                    contact_info,
                    tax_id,
                    address,
                    phone,
                    email,
                    payment_terms,
                    is_active
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING 
                    id,
                    tenant_id,
                    name,
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
            
            # Delete supplier
            await conn.execute("""
                DELETE FROM tenant_suppliers 
                WHERE id = $1 AND tenant_id = $2
            """, supplier_id, tenant_id)
            
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