"""Request observability helpers for high-demand incident review (#567)."""

from app.core.middleware import classify_request_severity, format_request_log


def test_classify_request_severity_separates_auth_and_errors():
    assert classify_request_severity(200, "/orders") == "ok"
    assert classify_request_severity(401, "/orders") == "auth_expected"
    assert classify_request_severity(404, "/orders/missing") == "client_error"
    assert classify_request_severity(500, "/orders") == "server_error"


def test_classify_request_severity_marks_notification_stream():
    assert classify_request_severity(200, "/notifications/stream") == "sse_stream"


def test_format_request_log_is_parseable_and_sanitized():
    line = format_request_log(
        method="GET",
        path="/orders",
        status_code=200,
        duration_ms=12.345,
        tenant="Demo Tenant",
        user_id="user with spaces",
    )

    assert line.startswith("request ")
    assert "method=GET" in line
    assert "path=/orders" in line
    assert "status_code=200" in line
    assert "duration_ms=12.35" in line
    assert "severity=ok" in line
    assert "tenant=Demo_Tenant" in line
    assert "user=user_with_spaces" in line
