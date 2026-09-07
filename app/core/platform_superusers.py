"""Platform superuser allowlist from env (no tenant_members required)."""

from __future__ import annotations

from functools import lru_cache
from typing import FrozenSet, Optional

from app.core.email_utils import normalize_email


def parse_platform_superuser_emails(raw: Optional[str]) -> FrozenSet[str]:
    """Parse comma-separated PLATFORM_SUPERUSER_EMAILS into normalized emails."""
    if not raw or not str(raw).strip():
        return frozenset()
    return frozenset(
        normalize_email(part)
        for part in str(raw).split(",")
        if part.strip()
    )


@lru_cache(maxsize=1)
def _allowlist_from_settings() -> FrozenSet[str]:
    from app.config import settings

    return parse_platform_superuser_emails(settings.platform_superuser_emails)


def clear_platform_superuser_cache() -> None:
    """Test helper — drop cached allowlist after monkeypatching settings."""
    _allowlist_from_settings.cache_clear()


def platform_superuser_emails() -> FrozenSet[str]:
    return _allowlist_from_settings()


def is_platform_superuser_email(email: Optional[str]) -> bool:
    if not email:
        return False
    return normalize_email(email) in platform_superuser_emails()


def platform_superuser_email_list() -> list[str]:
    """Sorted list for SQL ``<> ALL($n::text[])`` bindings."""
    return sorted(platform_superuser_emails())
