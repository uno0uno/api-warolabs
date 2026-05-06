"""
Address Profile Service
Business logic for customer delivery addresses (online ordering)
"""
from typing import Optional, List
from uuid import UUID
from fastapi import HTTPException
from app.database import get_db_connection
from app.core.exceptions import APIError
import logging

logger = logging.getLogger(__name__)

# Map Pydantic/API field names → real DB column names (for dynamic UPDATE)
FIELD_MAP = {
    "customer_id": "user_id",
    "address_type": "label",
}


async def create_address(
    customer_id: UUID,
    address_line1: str,
    city: str,
    state: str,
    postal_code: Optional[str] = None,
    address_line2: Optional[str] = None,
    country: str = "CO",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    is_default: bool = False,
    address_type: str = "home",
    delivery_notes: Optional[str] = None
) -> dict:
    """
    Create new delivery address for customer (PUBLIC)
    If is_default=True, unsets all other addresses as default
    Postal code is optional — coerce empty strings to None for DB consistency.
    """
    # Coerce empty postal_code to None so the column stores NULL, not "".
    postal_code = postal_code.strip() if postal_code and postal_code.strip() else None
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Enforce 5-address limit per customer
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) AS total FROM addresses_profile WHERE user_id = $1 AND deleted_at IS NULL",
                    customer_id
                )
                if count_row['total'] >= 5:
                    raise HTTPException(
                        status_code=422,
                        detail="Has alcanzado el límite de 5 direcciones. Elimina una antes de agregar otra."
                    )

                # If this is set as default, unset all other defaults for this customer
                if is_default:
                    await conn.execute(
                        "UPDATE addresses_profile SET is_default = false WHERE user_id = $1",
                        customer_id
                    )

                # Insert new address
                insert_query = """
                    INSERT INTO addresses_profile (
                        user_id, address_line1, address_line2, city, state,
                        postal_code, country, latitude, longitude, is_default,
                        label, delivery_notes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    RETURNING id, user_id AS customer_id, address_line1, address_line2, city, state,
                              postal_code, country, latitude, longitude, is_default,
                              label AS address_type, delivery_notes, created_at, updated_at
                """
                address_row = await conn.fetchrow(
                    insert_query,
                    customer_id,
                    address_line1,
                    address_line2,
                    city,
                    state,
                    postal_code,
                    country,
                    latitude,
                    longitude,
                    is_default,
                    address_type,
                    delivery_notes
                )

                logger.info(f"Created address {address_row['id']} for customer {customer_id}")
                return dict(address_row)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating address: {str(e)}")
        raise APIError(f"Error al crear dirección: {str(e)}", status_code=500)


async def get_customer_addresses(customer_id: UUID) -> dict:
    """
    Get all addresses for a customer (PUBLIC)
    Returns list of addresses with default address highlighted
    """
    try:
        async with get_db_connection() as conn:
            query = """
                SELECT id, user_id AS customer_id, address_line1, address_line2, city, state,
                       postal_code, country, latitude, longitude, is_default,
                       label AS address_type, delivery_notes, created_at, updated_at
                FROM addresses_profile
                WHERE user_id = $1 AND deleted_at IS NULL
                ORDER BY is_default DESC, created_at DESC
            """
            rows = await conn.fetch(query, customer_id)

            addresses = [dict(row) for row in rows]
            default_address_id = next(
                (addr['id'] for addr in addresses if addr['is_default']),
                None
            )

            return {
                "addresses": addresses,
                "total": len(addresses),
                "default_address_id": default_address_id
            }

    except Exception as e:
        logger.error(f"Error fetching addresses: {str(e)}")
        raise APIError(f"Error al obtener direcciones: {str(e)}", status_code=500)


async def get_address_by_id(address_id: UUID, customer_id: UUID) -> dict:
    """
    Get specific address by ID (PUBLIC)
    Validates that address belongs to customer
    """
    try:
        async with get_db_connection() as conn:
            query = """
                SELECT id, user_id AS customer_id, address_line1, address_line2, city, state,
                       postal_code, country, latitude, longitude, is_default,
                       label AS address_type, delivery_notes, created_at, updated_at
                FROM addresses_profile
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
            """
            address_row = await conn.fetchrow(query, address_id, customer_id)

            if not address_row:
                raise HTTPException(
                    status_code=404,
                    detail="Dirección no encontrada"
                )

            return dict(address_row)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching address: {str(e)}")
        raise APIError(f"Error al obtener dirección: {str(e)}", status_code=500)


async def update_address(
    address_id: UUID,
    customer_id: UUID,
    update_data: dict
) -> dict:
    """
    Update existing address (PUBLIC)
    Validates ownership and handles is_default flag.
    If postal_code arrives as empty string, coerce to None so the DB stores NULL.
    """
    if 'postal_code' in update_data:
        pc = update_data['postal_code']
        if isinstance(pc, str):
            pc = pc.strip()
            update_data['postal_code'] = pc if pc else None
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify ownership
                verify_query = "SELECT id FROM addresses_profile WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL"
                address_exists = await conn.fetchrow(verify_query, address_id, customer_id)

                if not address_exists:
                    raise HTTPException(
                        status_code=404,
                        detail="Dirección no encontrada"
                    )

                # If setting as default, unset all other defaults
                if update_data.get('is_default') is True:
                    await conn.execute(
                        "UPDATE addresses_profile SET is_default = false WHERE user_id = $1",
                        customer_id
                    )

                # Build dynamic update query
                update_fields = []
                update_values = []
                param_counter = 1

                for key, value in update_data.items():
                    if value is not None:  # Only update fields that are provided
                        db_col = FIELD_MAP.get(key, key)
                        update_fields.append(f"{db_col} = ${param_counter}")
                        update_values.append(value)
                        param_counter += 1

                if not update_fields:
                    # No fields to update, just return existing address
                    return await get_address_by_id(address_id, customer_id)

                # Add updated_at
                update_fields.append(f"updated_at = now()")

                # Add WHERE clause parameters
                update_values.append(address_id)
                update_values.append(customer_id)

                update_query = f"""
                    UPDATE addresses_profile
                    SET {', '.join(update_fields)}
                    WHERE id = ${param_counter} AND user_id = ${param_counter + 1}
                    RETURNING id, user_id AS customer_id, address_line1, address_line2, city, state,
                              postal_code, country, latitude, longitude, is_default,
                              label AS address_type, delivery_notes, created_at, updated_at
                """

                updated_row = await conn.fetchrow(update_query, *update_values)

                logger.info(f"Updated address {address_id} for customer {customer_id}")
                return dict(updated_row)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating address: {str(e)}")
        raise APIError(f"Error al actualizar dirección: {str(e)}", status_code=500)


async def delete_address(address_id: UUID, customer_id: UUID) -> dict:
    """
    Soft-delete address (PUBLIC)
    Sets deleted_at = now(). If it was the default, promotes the most recent remaining address.
    """
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Check ownership and not already deleted
                check_query = """
                    SELECT id, is_default
                    FROM addresses_profile
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                """
                address_row = await conn.fetchrow(check_query, address_id, customer_id)

                if not address_row:
                    raise HTTPException(status_code=404, detail="Dirección no encontrada")

                # Count remaining active addresses
                count_query = """
                    SELECT COUNT(*) AS total FROM addresses_profile
                    WHERE user_id = $1 AND deleted_at IS NULL
                """
                count_row = await conn.fetchrow(count_query, customer_id)
                total_addresses = count_row['total']

                # If deleting the default and others exist, promote the most recent one
                if address_row['is_default'] and total_addresses > 1:
                    # First unset current default to avoid unique constraint violation
                    await conn.execute(
                        "UPDATE addresses_profile SET is_default = false WHERE id = $1",
                        address_id
                    )
                    # Then promote the most recent remaining address
                    await conn.execute(
                        """
                        UPDATE addresses_profile
                        SET is_default = true
                        WHERE id = (
                            SELECT id FROM addresses_profile
                            WHERE user_id = $1 AND id != $2 AND deleted_at IS NULL
                            ORDER BY created_at DESC
                            LIMIT 1
                        )
                        """,
                        customer_id,
                        address_id
                    )

                # Soft delete
                await conn.execute(
                    "UPDATE addresses_profile SET deleted_at = now(), is_default = false WHERE id = $1",
                    address_id
                )

                logger.info(f"Soft-deleted address {address_id} for customer {customer_id}")
                return {
                    "success": True,
                    "message": "Dirección eliminada exitosamente",
                    "deleted_address_id": str(address_id)
                }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting address: {str(e)}")
        raise APIError(f"Error al eliminar dirección: {str(e)}", status_code=500)



async def set_default_address(address_id: UUID, customer_id: UUID) -> dict:
    """
    Set an address as default (PUBLIC)
    Unsets all other addresses as default for this customer
    """
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify ownership
                verify_query = "SELECT id FROM addresses_profile WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL"
                address_exists = await conn.fetchrow(verify_query, address_id, customer_id)

                if not address_exists:
                    raise HTTPException(
                        status_code=404,
                        detail="Dirección no encontrada"
                    )

                # Unset all defaults for this customer (active only)
                await conn.execute(
                    "UPDATE addresses_profile SET is_default = false WHERE user_id = $1 AND deleted_at IS NULL",
                    customer_id
                )

                # Set this address as default
                update_query = """
                    UPDATE addresses_profile
                    SET is_default = true, updated_at = now()
                    WHERE id = $1 AND user_id = $2
                    RETURNING id, user_id AS customer_id, address_line1, address_line2, city, state,
                              postal_code, country, latitude, longitude, is_default,
                              label AS address_type, delivery_notes, created_at, updated_at
                """
                updated_row = await conn.fetchrow(update_query, address_id, customer_id)

                logger.info(f"Set address {address_id} as default for customer {customer_id}")
                return dict(updated_row)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting default address: {str(e)}")
        raise APIError(f"Error al establecer dirección predeterminada: {str(e)}", status_code=500)
