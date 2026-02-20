"""
Address Profile Router
PUBLIC endpoints for customer delivery address management (online ordering)
"""
from fastapi import APIRouter, Body, Query
from uuid import UUID
from app.models.address_profile import (
    AddressProfileCreate,
    AddressProfileUpdate,
    AddressProfileResponse,
    AddressProfileList,
    AddressPreviewResponse,
)
from app.services import address_profile_service

router = APIRouter(prefix="/online/addresses", tags=["Online Addresses (Public)"])


@router.post("", response_model=AddressProfileResponse)
async def create_address(address: AddressProfileCreate):
    """
    Create new delivery address for customer (PUBLIC - no auth).

    - **customer_id**: Customer UUID (from OTP verification)
    - **address_line1**: Street address (required)
    - **address_line2**: Apartment, suite, etc (optional)
    - **city**: City name (required)
    - **state**: State/Department (required)
    - **postal_code**: Postal/ZIP code (required)
    - **country**: ISO country code (default: CO)
    - **latitude/longitude**: GPS coordinates (optional)
    - **is_default**: Set as default address (default: false)
    - **address_type**: home|work|other (default: home)
    - **delivery_notes**: Special instructions (optional)

    If `is_default=true`, all other addresses for this customer will be unset as default.

    **Public endpoint - no authentication required**
    """
    return await address_profile_service.create_address(
        customer_id=address.customer_id,
        address_line1=address.address_line1,
        address_line2=address.address_line2,
        city=address.city,
        state=address.state,
        postal_code=address.postal_code,
        country=address.country,
        latitude=address.latitude,
        longitude=address.longitude,
        is_default=address.is_default,
        address_type=address.address_type,
        delivery_notes=address.delivery_notes
    )


@router.get("/preview", response_model=AddressPreviewResponse)
async def preview_addresses_by_email(
    email: str = Query(..., description="Customer email to look up addresses for")
):
    """
    Read-only preview of saved addresses for a given email (PUBLIC - no auth).

    Used at the delivery step before OTP to show returning customers their
    saved addresses without requiring authentication.

    - **email**: Customer email address
    - Returns `customer_id` + list of addresses, or empty result if not found

    **Public endpoint - no authentication required**
    """
    return await address_profile_service.get_addresses_by_email(email)


@router.get("/customer/{customer_id}", response_model=AddressProfileList)
async def get_customer_addresses(customer_id: UUID):
    """
    Get all addresses for a customer (PUBLIC - no auth).

    Returns list of addresses ordered by:
    1. Default address first
    2. Most recent created

    Response includes:
    - **addresses**: List of all addresses
    - **total**: Total count
    - **default_address_id**: UUID of default address (if any)

    **Public endpoint - no authentication required**
    """
    return await address_profile_service.get_customer_addresses(customer_id)


@router.get("/{address_id}", response_model=AddressProfileResponse)
async def get_address(address_id: UUID, customer_id: UUID):
    """
    Get specific address by ID (PUBLIC - no auth).

    - **address_id**: Address UUID (path parameter)
    - **customer_id**: Customer UUID (query parameter for ownership validation)

    Returns 404 if address doesn't exist or doesn't belong to customer.

    **Public endpoint - no authentication required**
    """
    return await address_profile_service.get_address_by_id(address_id, customer_id)


@router.put("/{address_id}", response_model=AddressProfileResponse)
async def update_address(
    address_id: UUID,
    customer_id: UUID,
    address_update: AddressProfileUpdate
):
    """
    Update existing address (PUBLIC - no auth).

    - **address_id**: Address UUID (path parameter)
    - **customer_id**: Customer UUID (query parameter for ownership validation)
    - **address_update**: Fields to update (only provided fields will be updated)

    If `is_default=true`, all other addresses for this customer will be unset as default.

    Returns 404 if address doesn't exist or doesn't belong to customer.

    **Public endpoint - no authentication required**
    """
    # Convert Pydantic model to dict, excluding None values
    update_data = address_update.model_dump(exclude_none=True)

    return await address_profile_service.update_address(
        address_id=address_id,
        customer_id=customer_id,
        update_data=update_data
    )


@router.delete("/{address_id}")
async def delete_address(address_id: UUID, customer_id: UUID):
    """
    Delete address (PUBLIC - no auth).

    - **address_id**: Address UUID (path parameter)
    - **customer_id**: Customer UUID (query parameter for ownership validation)

    If deleting the default address and other addresses exist,
    the most recent address will be automatically set as default.

    Returns 404 if address doesn't exist or doesn't belong to customer.

    **Public endpoint - no authentication required**
    """
    return await address_profile_service.delete_address(address_id, customer_id)


@router.patch("/{address_id}/set-default", response_model=AddressProfileResponse)
async def set_default_address(address_id: UUID, customer_id: UUID):
    """
    Set address as default (PUBLIC - no auth).

    - **address_id**: Address UUID (path parameter)
    - **customer_id**: Customer UUID (query parameter for ownership validation)

    Unsets all other addresses as default for this customer.

    Returns 404 if address doesn't exist or doesn't belong to customer.

    **Public endpoint - no authentication required**
    """
    return await address_profile_service.set_default_address(address_id, customer_id)
