from typing import FrozenSet, Optional, Tuple


_NEXT_STEP_BY_STATE = {
    "email_verified": "business_profile",
    "business_profile_pending": "business_profile",
    "terms_pending": "terms",
    "starter_active": "setup",
    "payment_pending": "payment",
    "paid": "activation",
    "active": "setup",
    "setup_complete": None,
    "cancelled": None,
}


# Pending sessions are denied by default. Keep this list exact so adding a new
# route under /billing or /legal never grants it pre-payment by accident.
PENDING_SESSION_ROUTE_ALLOWLIST: FrozenSet[Tuple[str, str]] = frozenset({
    ("GET", "/auth/session"),
    ("POST", "/auth/signout"),
    ("GET", "/onboarding/status"),
    ("GET", "/onboarding/payment-status"),
    ("GET", "/billing/plans"),
    ("POST", "/billing/subscribe"),
    ("GET", "/billing/verify-payment"),
    ("GET", "/legal/terms/current"),
    ("GET", "/legal/terms/status"),
    ("POST", "/legal/terms/accept"),
})


def is_pending_session_route_allowed(method: str, path: str) -> bool:
    return (method.upper(), path.rstrip("/") or "/") in PENDING_SESSION_ROUTE_ALLOWLIST


def next_step_for_state(state: Optional[str]) -> Optional[str]:
    return _NEXT_STEP_BY_STATE.get(state)
