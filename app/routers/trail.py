"""Public first-party visit ingest (uno0uno/waro-trail#3).

Unauthenticated POST. Host calls waro_trail; does not reimplement trail_* schema.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from waro_trail import bootstrap, ingest_event

logger = logging.getLogger(__name__)

router = APIRouter()

SITE_KEY = "warocol.com"
MAX_BODY_BYTES = 8192
RATE_WINDOW_SECONDS = 60
RATE_LIMIT_PER_IP = 120
_rate_buckets: dict[str, list[float]] = defaultdict(list)

_SKIP_INGEST_ERROR = "trail ingest skipped"


class TrailEventBody(BaseModel):
    visitor_key: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=10000)
    site_key: str = Field(default=SITE_KEY, max_length=253)
    referrer: Optional[str] = Field(default=None, max_length=2048)
    utm_source: Optional[str] = Field(default=None, max_length=200)
    utm_medium: Optional[str] = Field(default=None, max_length=200)
    utm_campaign: Optional[str] = Field(default=None, max_length=200)
    utm_term: Optional[str] = Field(default=None, max_length=200)
    utm_content: Optional[str] = Field(default=None, max_length=200)

    @field_validator("visitor_key", "path", "site_key", mode="before")
    @classmethod
    def _strip_required(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


def trail_dsn() -> str:
    url = (settings.database_url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def bootstrap_trail_tables() -> None:
    bootstrap(trail_dsn())


def reset_rate_buckets() -> None:
    _rate_buckets.clear()


def _enforce_rate_limit(client_ip: Optional[str]) -> None:
    now = time.time()
    window_start = now - RATE_WINDOW_SECONDS
    key = f"ip:{client_ip or 'unknown'}"
    bucket = _rate_buckets[key]
    bucket[:] = [ts for ts in bucket if ts > window_start]
    if len(bucket) >= RATE_LIMIT_PER_IP:
        raise HTTPException(status_code=429, detail="Too many trail events")
    bucket.append(now)


def _payload_from_body(body: TrailEventBody, user_agent: Optional[str]) -> dict:
    payload = body.model_dump()
    payload["event_type"] = "page_view"
    payload["user_agent"] = user_agent
    return payload


async def _ingest(payload: dict) -> dict:
    return await asyncio.to_thread(ingest_event, trail_dsn(), payload)


def schedule_crawler_page_view(request: Request, slug: str) -> None:
    """Fire-and-forget page_view for non-JS crawlers hitting GET /blog/{slug}."""
    ua = request.headers.get("user-agent") or ""
    visitor_key = hashlib.sha256(ua.encode("utf-8")).hexdigest()[:32]
    payload = {
        "visitor_key": visitor_key,
        "site_key": SITE_KEY,
        "path": f"/blog/{slug}",
        "event_type": "page_view",
        "referrer": request.headers.get("referer"),
        "user_agent": ua or None,
    }
    asyncio.create_task(_ingest_crawler(payload))


async def _ingest_crawler(payload: dict) -> None:
    try:
        await _ingest(payload)
    except Exception:
        logger.warning(_SKIP_INGEST_ERROR, extra={"path": payload.get("path")})


@router.post("/events")
async def post_trail_event(request: Request, body: TrailEventBody):
    encoded = json.dumps(body.model_dump(), default=str)
    if len(encoded.encode("utf-8")) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    _enforce_rate_limit(request.client.host if request.client else None)

    user_agent = request.headers.get("user-agent")
    try:
        result = await _ingest(_payload_from_body(body, user_agent))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("trail ingest failed")
        raise HTTPException(status_code=503, detail="Trail ingest unavailable")

    return {
        "success": True,
        "session_id": str(result["session_id"]),
        "is_bot": bool(result.get("is_bot")),
    }
