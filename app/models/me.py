from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class AccessResponse(BaseModel):
    """Effective access map for the current session.

    Drives Epic 4 frontend sidebar / route gating. Surfaces three things
    derived from the session and tenant configuration:

    - `role`: the user's role string in the active tenant (None if the user
      has no membership row yet — e.g. fresh-tenant owner pre-bootstrap,
      KDS-token synthetic session).
    - `modules`: sorted list of module string values the user can access,
      already accounting for tenant-specific overrides and plan limits.
    - `plan_slug`: effective billing plan slug when a tenant is resolved.
    - `enforcement_mode`: the tenant's current permissions mode, one of
      'disabled' | 'shadow' | 'enforce'.
    - `features`: tenant feature capabilities that are not RBAC modules.
    """
    role: Optional[str] = None
    modules: List[str] = []
    plan_slug: Optional[str] = None
    enforcement_mode: str = "disabled"
    features: Dict[str, bool] = Field(
        default_factory=lambda: {"kali_enabled": False}
    )
