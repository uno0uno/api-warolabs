"""Restaurant Wompi collections API (#862). Dedicated webhook — not Tickets ingress."""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.permissions import Module, require_module
from app.services import wompi_collections_service

staff_router = APIRouter(prefix="/integraciones/pasarela", tags=["wompi-collections"])
session_router = APIRouter(prefix="/collections", tags=["wompi-collections"])
webhook_router = APIRouter(prefix="/collections/webhooks", tags=["wompi-collections"])


class ActivatePasarelaRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    public_key: str = Field(alias="publicKey")
    private_key: str = Field(alias="privateKey")
    events_secret: str = Field(alias="eventsSecret")
    integrity_secret: Optional[str] = Field(default=None, alias="integritySecret")


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    order_id: UUID = Field(alias="orderId")
    amount: Decimal
    customer_id: Optional[UUID] = Field(default=None, alias="customerId")
    link_email: Optional[str] = Field(default=None, alias="linkEmail")
    redirect_url: Optional[str] = Field(default=None, alias="redirectUrl")


class VerifySessionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transaction_id: Optional[str] = Field(default=None, alias="transactionId")


@staff_router.post("/activate", dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def activate_pasarela(request: Request, body: ActivatePasarelaRequest):
    return await wompi_collections_service.activate_merchant(
        request,
        public_key=body.public_key,
        private_key=body.private_key,
        events_secret=body.events_secret,
        integrity_secret=body.integrity_secret,
    )


@staff_router.get("", dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def pasarela_status(request: Request):
    return await wompi_collections_service.merchant_status(request)


@session_router.post("/sessions", dependencies=[Depends(require_module(Module.POS))])
async def create_session(request: Request, body: CreateSessionRequest):
    return await wompi_collections_service.create_collection_session(
        request,
        order_id=body.order_id,
        amount=body.amount,
        selected_customer_id=body.customer_id,
        link_email=body.link_email,
        redirect_url=body.redirect_url,
    )


@session_router.post("/sessions/{session_id}/verify")
async def verify_session(session_id: UUID, body: Optional[VerifySessionRequest] = None):
    transaction_id = body.transaction_id if body else None
    return await wompi_collections_service.verify_session(session_id, transaction_id)


@webhook_router.post("/wompi")
async def collections_wompi_webhook(request: Request):
    event_data = await request.json()
    return await wompi_collections_service.handle_collections_webhook(event_data)
