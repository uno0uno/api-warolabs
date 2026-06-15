"""
Publish WARO legal Terms and Conditions v1.1.

Default production usage expects the SSH tunnel and .env variables:
    ssh -L 5432:localhost:5432 warolabs -N
    python scripts/publish_legal_terms_v11.py

Dry run:
    python scripts/publish_legal_terms_v11.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.services.aws_s3_service import AWSS3Service  # noqa: E402
from app.services.legal_service import publish_terms_version  # noqa: E402


DEFAULT_PDF = Path("/Users/saifer/Documents/TyC_WARO_v1.1.pdf")
DEFAULT_VERSION = "1.1"
DEFAULT_R2_KEY = "legal/terms/TyC_WARO_v1.1.pdf"
DEFAULT_EFFECTIVE_AT = "2026-06-15T00:00:00-05:00"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish TyC WARO v1.1 to R2 and legal_document_versions")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--r2-key", default=DEFAULT_R2_KEY)
    parser.add_argument("--effective-at", default=DEFAULT_EFFECTIVE_AT)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--content-url", default=None, help="Use an already-uploaded public PDF URL and skip R2 upload")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _connect_kwargs() -> dict:
    return {
        "host": os.getenv("DB_HOST_OVERRIDE", "localhost"),
        "port": int(os.getenv("NUXT_PRIVATE_DB_PORT", "5432")),
        "user": os.getenv("NUXT_PRIVATE_DB_USER"),
        "password": os.getenv("NUXT_PRIVATE_DB_PASSWORD"),
        "database": os.getenv("NUXT_PRIVATE_DB_NAME"),
    }


async def _publish(args: argparse.Namespace) -> None:
    pdf_path = args.pdf.expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    pdf_sha256 = _sha256(pdf_bytes)
    effective_at = datetime.fromisoformat(args.effective_at)

    print(f"PDF: {pdf_path}")
    print(f"Version: {args.version}")
    print(f"SHA256: {pdf_sha256}")
    print(f"R2 key: {args.r2_key}")

    if args.dry_run:
        print("Dry run complete; no R2 upload or DB write performed.")
        return

    content_url = args.content_url
    if not content_url:
        content_url = await AWSS3Service().upload_public_asset(
            pdf_bytes,
            args.r2_key,
            "application/pdf",
            metadata={
                "source": pdf_path.name,
                "version": args.version,
                "sha256": pdf_sha256,
            },
        )
        if not content_url:
            raise RuntimeError("Failed to upload legal PDF to public R2")

    metadata = {
        "source": pdf_path.name,
        "status": "published",
        "display_mode": "pdf",
    }

    if args.database_url:
        conn = await asyncpg.connect(args.database_url)
    else:
        conn = await asyncpg.connect(**_connect_kwargs())

    try:
        async with conn.transaction():
            current = await publish_terms_version(
                conn,
                version=args.version,
                effective_at=effective_at,
                content_url=content_url,
                content_sha256=pdf_sha256,
                metadata=metadata,
            )
    finally:
        await conn.close()

    print("Published legal terms:")
    print(f"  version: {current['version']}")
    print(f"  content_url: {current['content_url']}")
    print(f"  content_sha256: {current['content_sha256']}")


def main() -> None:
    asyncio.run(_publish(_parse_args()))


if __name__ == "__main__":
    main()
