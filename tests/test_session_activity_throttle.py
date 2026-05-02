"""
Regression guard for issue #146 — the per-request UPDATE on
`sessions.last_activity_at` must be conditional, writing only when the stored
timestamp is older than SESSION_ACTIVITY_THROTTLE. If the conditional WHERE
ever regresses to the unconditional 2-arm shape, mesa-time bursty POS traffic
will reproduce the original 65 ms / 78%-of-DB-time slowdown.

These tests are static — they parse the source files and assert the SQL shape.
A live DB-roundtrip test would require auth seeding the smoke harness doesn't
provide; static checks catch the regression that matters (someone reverting the
WHERE clause) without needing the full integration stack.
"""
import pathlib
import re

from app.core.security import SESSION_ACTIVITY_THROTTLE


SECURITY_PY = pathlib.Path("app/core/security.py")
AUTH_SERVICE_PY = pathlib.Path("app/services/auth_service.py")


def test_throttle_constant_exists_and_is_a_postgres_interval_literal():
    # Constant must be a string Postgres can parse inside INTERVAL '...'.
    # Allowed shapes: "<n> seconds", "<n> minutes", "<n> hours". Reject anything
    # that doesn't fit so a typo can't silently disable the throttle.
    assert isinstance(SESSION_ACTIVITY_THROTTLE, str)
    assert re.fullmatch(
        r"\d+\s+(seconds?|minutes?|hours?)",
        SESSION_ACTIVITY_THROTTLE,
    ), (
        f"SESSION_ACTIVITY_THROTTLE={SESSION_ACTIVITY_THROTTLE!r} is not a valid "
        "Postgres INTERVAL literal — must look like '30 seconds'."
    )


def test_security_py_uses_conditional_update():
    src = SECURITY_PY.read_text()
    # Pattern must include both the SET and the time-bounded WHERE.
    assert "UPDATE sessions" in src
    assert "SET last_activity_at = NOW()" in src
    assert "last_activity_at < NOW() - INTERVAL" in src, (
        "app/core/security.py lost the conditional throttle on the "
        "`UPDATE sessions SET last_activity_at` write. This re-introduces "
        "the per-request write storm — see issue #146."
    )


def test_auth_service_py_uses_conditional_update():
    src = AUTH_SERVICE_PY.read_text()
    assert "UPDATE sessions" in src
    assert "SET last_activity_at = NOW()" in src
    assert "last_activity_at < NOW() - INTERVAL" in src, (
        "app/services/auth_service.py lost the conditional throttle on the "
        "`UPDATE sessions SET last_activity_at` write. This re-introduces "
        "the per-request write storm — see issue #146."
    )


def test_no_unconditional_session_activity_update_remains_in_codebase():
    """
    Belt-and-suspenders: scan both files for the legacy 2-arm shape.
    The legacy shape is:
        UPDATE sessions SET last_activity_at = NOW() WHERE id = $X
    with no `AND last_activity_at < ...` follow-up. We catch that by checking
    every line that mentions the SET clause and ensuring the surrounding
    statement (within 5 lines forward) also includes the conditional WHERE.
    """
    for path in (SECURITY_PY, AUTH_SERVICE_PY):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if "SET last_activity_at = NOW()" not in line:
                continue
            window = "\n".join(lines[i : i + 5])
            assert "last_activity_at < NOW() - INTERVAL" in window, (
                f"{path}:{i + 1} contains an unconditional "
                f"UPDATE sessions SET last_activity_at = NOW() — must be "
                f"throttled (see issue #146): {line.strip()}"
            )
