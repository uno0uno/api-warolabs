from typing import List, Optional
from pydantic import BaseModel


class AccessResponse(BaseModel):
    """Effective access map for the current session.

    Drives Epic 4 frontend sidebar / route gating. Surfaces three things
    derived from the session and tenant configuration:

    - `role`: the user's role string in the active tenant (None if the user
      has no membership row yet — e.g. fresh-tenant owner pre-bootstrap,
      KDS-token synthetic session).
    - `modules`: sorted list of module string values the user can access,
      already accounting for tenant-specific overrides.
    - `enforcement_mode`: the tenant's current permissions mode, one of
      'disabled' | 'shadow' | 'enforce'.
    """
    role: Optional[str] = None
    modules: List[str] = []
    enforcement_mode: str = "disabled"
