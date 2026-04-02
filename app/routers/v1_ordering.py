"""
V1 Ordering Router
Cart management endpoints authenticated via API key.
tenant_id is injected from the API key context — never exposed to callers.
"""
from fastapi import APIRouter, Request
from typing import List, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from app.services import online_cart_service
from app.services.public_api_service import validate_api_key_auth
from app.models.online_cart import OnlineCartItemCreate, DeliveryInfoUpdate

router = APIRouter(prefix="/v1/cart", tags=["V1 Ordering"])


class V1BatchCreateCartRequest(BaseModel):
    """Create cart with batch items — tenant_id injected from API key"""
    items: List[OnlineCartItemCreate]
    session_id: Optional[str] = None
    order_type: str = Field(default='delivery', pattern='^(delivery|pickup|dine-in)$')


@router.post("/batch")
async def create_cart_batch(request: Request, body: V1BatchCreateCartRequest):
    """
    Create cart and add all items in batch.

    tenant_id is inferred from the API key — callers never need to provide it.

    **Authentication required:** `Authorization: Bearer waro_sk_xxx` or `X-API-Key: waro_sk_xxx`
    **Scope required:** `read`
    """
    tenant_id, _ = validate_api_key_auth(request, "read")
    return await online_cart_service.create_cart_with_batch_items(
        tenant_id=UUID(tenant_id),
        items=[item.dict() for item in body.items],
        session_id=body.session_id,
        order_type=body.order_type,
    )


@router.get("/session/{session_id}")
async def get_cart_by_session(session_id: str, request: Request):
    """
    Get active cart by session ID.

    tenant_id is inferred from the API key — no query param needed.

    **Authentication required:** `Authorization: Bearer waro_sk_xxx` or `X-API-Key: waro_sk_xxx`
    **Scope required:** `read`
    """
    tenant_id, _ = validate_api_key_auth(request, "read")
    return await online_cart_service.get_cart_by_session(
        session_id=session_id,
        tenant_id=UUID(tenant_id),
    )


@router.put("/{cart_id}/delivery")
async def update_delivery_info(cart_id: UUID, request: Request, delivery_info: DeliveryInfoUpdate):
    """
    Update delivery information for a cart.

    **Authentication required:** `Authorization: Bearer waro_sk_xxx` or `X-API-Key: waro_sk_xxx`
    **Scope required:** `read`
    """
    validate_api_key_auth(request, "read")
    return await online_cart_service.update_delivery_info(
        cart_id=cart_id,
        order_type=delivery_info.order_type,
        delivery_address_id=delivery_info.delivery_address_id,
        scheduled_time=delivery_info.scheduled_time if delivery_info.scheduled_time else None,
        delivery_instructions=delivery_info.delivery_instructions,
    )


@router.delete("/{cart_id}/items/{item_id}")
async def delete_cart_item(cart_id: UUID, item_id: UUID, request: Request):
    """
    Delete a specific item from a cart.

    **Authentication required:** `Authorization: Bearer waro_sk_xxx` or `X-API-Key: waro_sk_xxx`
    **Scope required:** `read`
    """
    validate_api_key_auth(request, "read")
    return await online_cart_service.delete_cart_item(cart_id=cart_id, item_id=item_id)


@router.delete("/{cart_id}")
async def clear_cart(cart_id: UUID, request: Request):
    """
    Clear all items from a cart.

    **Authentication required:** `Authorization: Bearer waro_sk_xxx` or `X-API-Key: waro_sk_xxx`
    **Scope required:** `read`
    """
    validate_api_key_auth(request, "read")
    return await online_cart_service.clear_cart(cart_id=cart_id)
