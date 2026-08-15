"""Public trail ingest contract (uno0uno/waro-trail#3)."""
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import trail as trail_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(trail_router.router, prefix="/public/trail")
    return TestClient(app)


def setup_function():
    trail_router.reset_rate_buckets()


def test_rejects_missing_path():
    client = _client()
    response = client.post("/public/trail/events", json={"visitor_key": "abc"})
    assert response.status_code == 422


def test_rejects_oversized_payload():
    client = _client()
    huge = "x" * (trail_router.MAX_BODY_BYTES + 10)
    response = client.post(
        "/public/trail/events",
        json={"visitor_key": "abc", "path": f"/{huge}"},
    )
    assert response.status_code == 413


def test_accepts_page_view_via_library():
    session_id = uuid4()
    client = _client()
    with patch(
        "app.routers.trail.ingest_event",
        return_value={"session_id": session_id, "is_bot": False, "bot_family": None},
    ) as ingest:
        response = client.post(
            "/public/trail/events",
            json={
                "visitor_key": "opaque-id",
                "path": "/blog/demo",
                "referrer": "https://google.com/",
                "utm_source": "google",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["session_id"] == str(session_id)
    assert ingest.called
    payload = ingest.call_args.args[1]
    assert payload["visitor_key"] == "opaque-id"
    assert payload["path"] == "/blog/demo"
    assert payload["site_key"] == "warocol.com"
    assert payload["event_type"] == "page_view"
    assert "email" not in payload


def test_forwards_scroll_depth_and_page_leave():
    session_id = uuid4()
    client = _client()
    with patch(
        "app.routers.trail.ingest_event",
        return_value={"session_id": session_id, "is_bot": False, "bot_family": None},
    ) as ingest:
        scroll = client.post(
            "/public/trail/events",
            json={
                "visitor_key": "opaque-id",
                "path": "/blog/demo",
                "event_type": "scroll_depth",
                "scroll_pct": 75,
            },
        )
        leave = client.post(
            "/public/trail/events",
            json={
                "visitor_key": "opaque-id",
                "path": "/blog/demo",
                "event_type": "page_leave",
                "scroll_pct": 100,
                "dwell_ms": 12000,
            },
        )
    assert scroll.status_code == 200
    assert leave.status_code == 200
    scroll_payload = ingest.call_args_list[0].args[1]
    leave_payload = ingest.call_args_list[1].args[1]
    assert scroll_payload["event_type"] == "scroll_depth"
    assert scroll_payload["scroll_pct"] == 75
    assert leave_payload["event_type"] == "page_leave"
    assert leave_payload["scroll_pct"] == 100
    assert leave_payload["dwell_ms"] == 12000


def test_rejects_unknown_event_type_and_bad_scroll():
    client = _client()
    unknown = client.post(
        "/public/trail/events",
        json={"visitor_key": "a", "path": "/blog/demo", "event_type": "heartbeat"},
    )
    bad_scroll = client.post(
        "/public/trail/events",
        json={
            "visitor_key": "a",
            "path": "/blog/demo",
            "event_type": "scroll_depth",
            "scroll_pct": 101,
        },
    )
    assert unknown.status_code == 422
    assert bad_scroll.status_code == 422


def test_rate_limit_returns_429():
    client = _client()
    original = trail_router.RATE_LIMIT_PER_IP
    trail_router.RATE_LIMIT_PER_IP = 2
    try:
        with patch(
            "app.routers.trail.ingest_event",
            return_value={"session_id": uuid4(), "is_bot": False},
        ):
            assert client.post(
                "/public/trail/events",
                json={"visitor_key": "a", "path": "/blog"},
            ).status_code == 200
            assert client.post(
                "/public/trail/events",
                json={"visitor_key": "a", "path": "/blog/b"},
            ).status_code == 200
            third = client.post(
                "/public/trail/events",
                json={"visitor_key": "a", "path": "/blog/c"},
            )
            assert third.status_code == 429
    finally:
        trail_router.RATE_LIMIT_PER_IP = original
