"""Storefront slug helpers (api-warolabs#832).

Name → URL slug for public profiles; opaque conflicts (no hints).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from fastapi import HTTPException

# Contingency aliases only for provisional registration slugs.
ONBOARDING_SLUG_PREFIX = "onboarding-"

OPAQUE_IDENTITY_CONFLICT = {
    "code": "BUSINESS_IDENTITY_UNAVAILABLE",
    "message": "Choose a different business name.",
}

# Must not collide with static /api/public/restaurant/* routes.
RESERVED_STOREFRONT_SLUGS = frozenset({"list", "cities"})


def slugify_business_name(name: str) -> str:
    """Lowercase, spaces/separators → `-`, strip unsafe chars."""
    raw = unicodedata.normalize("NFKD", str(name or ""))
    ascii_only = raw.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    lowered = re.sub(r"[\s_]+", "-", lowered)
    lowered = re.sub(r"[^a-z0-9-]", "", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    if not lowered or lowered in RESERVED_STOREFRONT_SLUGS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BUSINESS_NAME_INVALID",
                "message": "Choose a different business name.",
            },
        )
    return lowered


def is_onboarding_provisional_slug(slug: Optional[str]) -> bool:
    return bool(slug) and str(slug).startswith(ONBOARDING_SLUG_PREFIX)


def raise_opaque_identity_conflict() -> None:
    """Conflict without leaking which name/slug exists."""
    raise HTTPException(status_code=409, detail=OPAQUE_IDENTITY_CONFLICT)
