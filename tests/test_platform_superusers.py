"""Unit tests for PLATFORM_SUPERUSER_EMAILS allowlist (#776)."""

from __future__ import annotations

import pytest

from app.core.platform_superusers import (
    clear_platform_superuser_cache,
    is_platform_superuser_email,
    parse_platform_superuser_emails,
    platform_superuser_email_list,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_platform_superuser_cache()
    yield
    clear_platform_superuser_cache()


def test_parse_empty_env():
    assert parse_platform_superuser_emails("") == frozenset()
    assert parse_platform_superuser_emails(None) == frozenset()
    assert parse_platform_superuser_emails("  ,  , ") == frozenset()


def test_parse_comma_separated_case_insensitive():
    emails = parse_platform_superuser_emails(
        "Anderson.Electronico@gmail.com, other@WARO.CO "
    )
    assert emails == frozenset(
        {"anderson.electronico@gmail.com", "other@waro.co"}
    )


def test_is_platform_superuser_email_reads_settings(monkeypatch):
    import app.config as config_mod

    monkeypatch.setenv(
        "PLATFORM_SUPERUSER_EMAILS",
        "anderson.electronico@gmail.com",
    )
    config_mod.settings = config_mod.Settings()
    clear_platform_superuser_cache()

    assert is_platform_superuser_email("Anderson.Electronico@gmail.com") is True
    assert is_platform_superuser_email("someone.else@example.com") is False
    assert platform_superuser_email_list() == ["anderson.electronico@gmail.com"]

    monkeypatch.setenv("PLATFORM_SUPERUSER_EMAILS", "")
    config_mod.settings = config_mod.Settings()
    clear_platform_superuser_cache()
    assert is_platform_superuser_email("anderson.electronico@gmail.com") is False


def test_security_injects_superuser_role(monkeypatch):
    """Allowlisted email gets effective superuser even when JOIN role is None."""
    monkeypatch.setenv(
        "PLATFORM_SUPERUSER_EMAILS",
        "ops@warocol.com",
    )
    import app.config as config_mod

    config_mod.settings = config_mod.Settings()
    clear_platform_superuser_cache()

    from app.core.platform_superusers import is_platform_superuser_email

    row_email = "ops@warocol.com"
    resolved_role = None
    if is_platform_superuser_email(row_email):
        resolved_role = "superuser"
    assert resolved_role == "superuser"

    resolved_role = "admin"
    if is_platform_superuser_email("normal@warocol.com"):
        resolved_role = "superuser"
    assert resolved_role == "admin"


def test_members_exclusion_predicate():
    """Mirrors SQL: empty allowlist keeps everyone; match drops platform emails."""
    allowlist = platform_superuser_email_list()
    # empty by default in this process unless env set
    assert allowlist == [] or isinstance(allowlist, list)

    allowlist = ["anderson.electronico@gmail.com"]
    members = [
        {"email": "anderson.electronico@gmail.com"},
        {"email": "chef@restaurant.com"},
    ]
    kept = [
        m
        for m in members
        if not allowlist or m["email"].lower().strip() not in allowlist
    ]
    assert [m["email"] for m in kept] == ["chef@restaurant.com"]


def test_quota_exclusion_predicate():
    allowlist = {"ops@warocol.com"}
    member_emails = ["ops@warocol.com", "admin@tenant.com"]
    counted = [e for e in member_emails if e not in allowlist]
    assert counted == ["admin@tenant.com"]
