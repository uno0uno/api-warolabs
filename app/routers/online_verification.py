"""
Online Verification Router
PUBLIC endpoints for OTP verification and customer validation (NO authentication required)
"""
from fastapi import APIRouter, Body
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, EmailStr
from app.services import otp_service

router = APIRouter(prefix="/online/otp", tags=["Online Verification (Public)"])


class SendOTPRequest(BaseModel):
    """Send OTP to email"""
    email: EmailStr
    cart_id: UUID


class VerifyOTPRequest(BaseModel):
    """Verify OTP code"""
    email: EmailStr
    cart_id: UUID
    otp_code: str


class ResendOTPRequest(BaseModel):
    """Resend OTP"""
    email: EmailStr
    cart_id: UUID


class ValidateCustomerRequest(BaseModel):
    """Validate customer eligibility"""
    phone_number: str
    cart_total: float = 0.0


@router.post("/send")
async def send_otp(request: SendOTPRequest):
    """
    Send OTP code via email (PUBLIC - no auth).

    - email: Customer email address
    - cart_id: Cart UUID to associate with verification

    Returns OTP expiration time and success message.

    **Public endpoint - no authentication required**
    """
    return await otp_service.send_otp_email(
        email=request.email,
        cart_id=request.cart_id
    )


@router.post("/verify")
async def verify_otp(request: VerifyOTPRequest):
    """
    Verify OTP code (PUBLIC - no auth).

    - email: Customer email address
    - cart_id: Cart UUID
    - otp_code: 6-digit verification code

    Returns customer_id and optional pickup_pin if order type is pickup.

    Maximum 3 attempts per OTP code.
    Codes expire after 5 minutes.

    **Public endpoint - no authentication required**
    """
    return await otp_service.verify_otp_code(
        email=request.email,
        cart_id=request.cart_id,
        otp_code=request.otp_code
    )


@router.post("/resend")
async def resend_otp(request: ResendOTPRequest):
    """
    Resend OTP code (PUBLIC - no auth).

    - email: Customer email address
    - cart_id: Cart UUID

    Cooldown: 60 seconds between resends.

    **Public endpoint - no authentication required**
    """
    # Uses the same send_otp_email function which handles cooldown
    return await otp_service.send_otp_email(
        email=request.email,
        cart_id=request.cart_id
    )


# Customer validation endpoint (separate from OTP)
customer_router = APIRouter(prefix="/online/customer", tags=["Online Customer Validation (Public)"])


@customer_router.post("/validate")
async def validate_customer(request: ValidateCustomerRequest):
    """
    Validate customer eligibility to order (PUBLIC - no auth).

    Checks:
    - Blacklist status
    - Customer tier (new/intermediate/trusted)
    - Spending limits based on order history

    Returns:
    - can_order: bool - Whether customer can place order
    - is_blacklisted: bool - Blacklist status
    - customer_tier: str - new|intermediate|trusted
    - max_amount: float - Maximum allowed order amount (if limited)
    - warnings: list - Any warnings about customer status

    **Public endpoint - no authentication required**
    """
    return await otp_service.validate_customer(
        phone_number=request.phone_number,
        cart_total=request.cart_total
    )
