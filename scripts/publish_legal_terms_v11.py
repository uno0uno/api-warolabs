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
import html
import os
import subprocess
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


def _extract_pdf_text(pdf_path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdftotext is required to extract legal PDF content") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pdftotext failed: {exc.stderr.strip()}") from exc
    return result.stdout


def _is_heading(text: str) -> bool:
    compact = text.strip()
    if not compact or len(compact) > 140:
        return False
    letters = [ch for ch in compact if ch.isalpha()]
    if len(letters) < 4:
        return False
    uppercase_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    return uppercase_ratio > 0.75 and not compact.endswith(".")


def build_body_html_from_text(text: str) -> str:
    """
    Convert pdftotext output to safe, readable HTML for the current frontend.

    This intentionally escapes all text and preserves paragraphs/headings instead
    of trusting arbitrary HTML.
    """
    blocks: list[str] = []
    paragraph: list[str] = []
    first_heading = True

    def flush_paragraph() -> None:
        if not paragraph:
            return
        value = " ".join(part.strip() for part in paragraph if part.strip())
        paragraph.clear()
        if value:
            blocks.append(f"<p>{html.escape(value)}</p>")

    for raw_line in text.replace("\f", "\n\n").splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if line.startswith("Página ") and line[7:].isdigit():
            flush_paragraph()
            continue

        if _is_heading(line):
            flush_paragraph()
            tag = "h1" if first_heading else "h2"
            first_heading = False
            blocks.append(f"<{tag}>{html.escape(line)}</{tag}>")
            continue

        paragraph.append(line)

    flush_paragraph()
    return "\n".join(blocks)


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
    body_html = build_body_html_from_text(_extract_pdf_text(pdf_path))
    effective_at = datetime.fromisoformat(args.effective_at)

    print(f"PDF: {pdf_path}")
    print(f"Version: {args.version}")
    print(f"SHA256: {pdf_sha256}")
    print(f"R2 key: {args.r2_key}")
    print(f"HTML chars: {len(body_html)}")

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
        "body_html": body_html,
        "status": "published",
        "privacy_policy_url": "#politica-datos-personales",
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
