"""Menu / bodega CSV bulk import jobs (#2254)."""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.ingredient import PurchaseUnitInput, TenantIngredientCreate
from app.services.aws_s3_service import AWSS3Service
from app.services.billing_service import (
    check_plan_quota_growth,
    check_plan_quota_scoped,
    preview_plan_quota_growth,
)
from app.services.ingredient_purchase_units_service import resolve_to_base_unit
from app.services.ingredients_service import create_tenant_ingredient

logger = logging.getLogger(__name__)

WAREHOUSE_TEMPLATE_HEADERS = [
    "name",
    "unit",
    "type",
    "category",
    "costo_unitario",
    "is_resale",
    "unit_weight_gr",
    "unit_weight_unit",
    "create_product",
    "price",
    "menu_category",
]

RETENTION_DAYS = 90


def warehouse_csv_template_bytes() -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=WAREHOUSE_TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow(
        {
            "name": "Coca-Cola 350ml",
            "unit": "und",
            "type": "food",
            "category": "Bebidas",
            "costo_unitario": "1200",
            "is_resale": "true",
            "unit_weight_gr": "350",
            "unit_weight_unit": "ml",
            "create_product": "true",
            "price": "3500",
            "menu_category": "Bebidas",
        }
    )
    writer.writerow(
        {
            "name": "Tomate",
            "unit": "kg",
            "type": "food",
            "category": "Verduras",
            "costo_unitario": "4000",
            "is_resale": "false",
            "unit_weight_gr": "",
            "unit_weight_unit": "",
            "create_product": "false",
            "price": "",
            "menu_category": "",
        }
    )
    return buf.getvalue().encode("utf-8-sig")


def _truthy(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "si", "sí"}


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except ValueError:
        return None


def _parse_decimal(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _normalize_row(raw: Dict[str, str]) -> Dict[str, Any]:
    return {((k or "").strip().lower()): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}


async def _resolve_menu_category_id(conn, tenant_id: UUID, name: str) -> Optional[UUID]:
    row = await conn.fetchrow(
        """
        SELECT id FROM categories
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return row["id"] if row else None


async def _ingredient_name_exists(conn, tenant_id: UUID, name: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM ingredients
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
          AND COALESCE(is_active, true) = true
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return bool(row)


async def create_resale_product_for_ingredient(
    conn,
    tenant_id: UUID,
    *,
    ingredient_id: UUID,
    name: str,
    price: Decimal,
    category_id: UUID,
    is_available: bool = True,
) -> UUID:
    """Warehouse-first: link existing und/is_resale ingredient to a sellable product (qty 1)."""
    await check_plan_quota_growth(conn, tenant_id, "menu_products")
    product_result = await conn.fetchrow(
        """
        INSERT INTO product (
            name, description, price, category_id, product_base_type_id, preparation_time,
            controla_stock, is_available, is_available_online, is_available_table_qr,
            is_combo, is_resale, open_priced, allow_modifiers,
            tax_category, tax_resolution, tax_line_key,
            tenant_id, station_id, kitchen_name, image_url, costo_percibido
        )
        VALUES (
            $1, NULL, $2, $3, NULL, NULL,
            true, $4, false, false,
            false, true, false, false,
            'standard', 'inherit', NULL,
            $5, NULL, NULL, NULL, NULL
        )
        RETURNING id
        """,
        name,
        price,
        category_id,
        is_available,
        tenant_id,
    )
    product_id = product_result["id"]
    await check_plan_quota_scoped(
        conn,
        tenant_id,
        "recipe_lines_per_product",
        product_id,
        projected_count=1,
    )
    base_qty, base_unit = await resolve_to_base_unit(conn, ingredient_id, 1, "und")
    await conn.execute(
        """
        INSERT INTO product_recipes (product_id, ingredient_id, quantity, unit, tenant_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        product_id,
        ingredient_id,
        base_qty,
        base_unit,
        tenant_id,
    )
    return product_id


def validate_warehouse_row(
    row: Dict[str, Any],
    row_num: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (normalized_ok, error_dict)."""
    name = (row.get("name") or "").strip()
    if not name:
        return None, {"row": row_num, "field": "name", "error": "name is required"}

    unit = (row.get("unit") or "").strip() or "und"
    is_resale = _truthy(row.get("is_resale"))
    unit_weight_gr = _parse_float(row.get("unit_weight_gr"))
    unit_weight_unit = (row.get("unit_weight_unit") or "gr").strip().lower() or "gr"
    if unit_weight_unit not in {"gr", "ml"}:
        return None, {"row": row_num, "field": "unit_weight_unit", "error": "must be gr or ml"}

    if is_resale and unit != "und":
        return None, {
            "row": row_num,
            "field": "unit",
            "error": "resale ingredients must use unit und",
        }
    if is_resale and (unit_weight_gr is None or unit_weight_gr <= 0):
        return None, {
            "row": row_num,
            "field": "unit_weight_gr",
            "error": "required for resale (positive number)",
        }

    create_product = _truthy(row.get("create_product"))
    if create_product and not is_resale:
        return None, {
            "row": row_num,
            "field": "create_product",
            "error": "create_product requires is_resale=true",
        }

    price = _parse_decimal(row.get("price"))
    menu_category = (row.get("menu_category") or "").strip()
    needs_product = False
    will_create_product = False
    if is_resale and create_product:
        if price is not None and price > 0 and menu_category:
            will_create_product = True
        else:
            # Decision A: ingredient only; finish in #2256
            needs_product = True

    if price is not None and price <= 0:
        return None, {"row": row_num, "field": "price", "error": "price must be > 0 when set"}

    ing_type = (row.get("type") or "food").strip().lower() or "food"
    if ing_type not in {"food", "supply", "service"}:
        return None, {"row": row_num, "field": "type", "error": "must be food|supply|service"}

    costo = _parse_float(row.get("costo_unitario"))
    if row.get("costo_unitario") not in (None, "") and costo is None:
        return None, {"row": row_num, "field": "costo_unitario", "error": "invalid number"}

    return {
        "row": row_num,
        "name": name,
        "unit": unit,
        "type": ing_type,
        "category": (row.get("category") or "").strip() or None,
        "costo_unitario": costo,
        "is_resale": is_resale,
        "unit_weight_gr": unit_weight_gr,
        "unit_weight_unit": unit_weight_unit,
        "create_product": create_product,
        "price": float(price) if price is not None else None,
        "menu_category": menu_category or None,
        "needs_product": needs_product,
        "will_create_product": will_create_product,
    }, None


def parse_warehouse_csv(content: bytes) -> List[Dict[str, str]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV has no header row")
    rows = []
    for raw in reader:
        rows.append(_normalize_row(raw))
    return rows


async def upload_warehouse_import(
    request: Request,
    file: UploadFile,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="Only .csv files are supported in v1")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="File too large (max 5MB)")

    try:
        parse_warehouse_csv(raw)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid CSV: {exc}") from exc

    s3 = AWSS3Service()
    s3_key = await s3.upload_file(
        file_content=io.BytesIO(raw),
        filename=file.filename,
        folder=f"menu/imports/{tenant_id}",
        content_type=file.content_type or "text/csv",
    )
    if not s3_key:
        raise HTTPException(status_code=500, detail="Failed to store import file")

    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            """
            INSERT INTO menu_import_jobs (
                tenant_id, uploaded_by, entity_type, status,
                file_name, mime_type, file_size, s3_key
            )
            VALUES ($1, $2, 'warehouse', 'uploaded', $3, $4, $5, $6)
            RETURNING id, status, file_name, created_at
            """,
            tenant_id,
            user_id,
            file.filename,
            file.content_type or "text/csv",
            len(raw),
            s3_key,
        )

    return {
        "success": True,
        "data": {
            "id": str(job["id"]),
            "status": job["status"],
            "file_name": job["file_name"],
            "entity_type": "warehouse",
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        },
    }


async def _load_job_bytes(conn, job: Dict[str, Any]) -> bytes:
    s3_key = job.get("s3_key")
    if not s3_key:
        raise HTTPException(status_code=404, detail="Import file missing")
    s3 = AWSS3Service()
    data = await s3.get_object_bytes(s3_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Import file not found in storage")
    return data


async def dry_run_warehouse_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            """
            SELECT * FROM menu_import_jobs
            WHERE id = $1 AND tenant_id = $2 AND entity_type = 'warehouse'
            """,
            job_id,
            tenant_id,
        )
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found")
        if job["status"] == "committed":
            raise HTTPException(status_code=409, detail="Job already committed")

        raw = await _load_job_bytes(conn, dict(job))
        rows = parse_warehouse_csv(raw)

        valid: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        seen_names = set()

        for idx, row in enumerate(rows, start=2):
            ok, err = validate_warehouse_row(row, idx)
            if err:
                errors.append(err)
                continue
            assert ok is not None
            key = ok["name"].lower()
            if key in seen_names:
                errors.append({"row": idx, "field": "name", "error": "duplicate name in file"})
                continue
            seen_names.add(key)
            if await _ingredient_name_exists(conn, tenant_id, ok["name"]):
                errors.append({"row": idx, "field": "name", "error": "ingredient already exists"})
                continue
            if ok["will_create_product"]:
                cat_id = await _resolve_menu_category_id(conn, tenant_id, ok["menu_category"])
                if not cat_id:
                    errors.append(
                        {
                            "row": idx,
                            "field": "menu_category",
                            "error": f"menu category '{ok['menu_category']}' not found",
                        }
                    )
                    continue
                ok["menu_category_id"] = str(cat_id)
            valid.append(ok)

        will_create_product_count = sum(1 for v in valid if v.get("will_create_product"))
        quota_hits: List[Dict[str, Any]] = []
        for resource, additional in (
            ("tenant_ingredients", len(valid)),
            ("menu_products", will_create_product_count),
        ):
            hit = await preview_plan_quota_growth(
                conn, tenant_id, resource, additional=additional
            )
            if hit:
                quota_hits.append(hit)
                errors.append(
                    {
                        "row": None,
                        "field": "quota",
                        "error": (
                            f"{resource}: projected {hit['projected']} exceeds limit "
                            f"{hit['limit']} (used {hit['used']} + {hit['additional']})"
                        ),
                    }
                )

        report = {
            "valid": valid,
            "errors": errors,
            "needs_product_count": sum(1 for v in valid if v.get("needs_product")),
            "will_create_product_count": will_create_product_count,
            "quota_hits": quota_hits,
            "quota_exceeded": bool(quota_hits),
        }

        await conn.execute(
            """
            UPDATE menu_import_jobs
            SET status = 'dry_run',
                row_total = $2,
                row_valid = $3,
                row_invalid = $4,
                dry_run_report = $5::jsonb,
                updated_at = NOW(),
                error_message = NULL
            WHERE id = $1
            """,
            job_id,
            len(rows),
            len(valid),
            len(errors),
            json.dumps(report),
        )

    return {
        "success": True,
        "data": {
            "id": str(job_id),
            "status": "dry_run",
            "row_total": len(rows),
            "row_valid": len(valid),
            "row_invalid": len(errors),
            "report": report,
        },
    }


async def commit_warehouse_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                SELECT * FROM menu_import_jobs
                WHERE id = $1 AND tenant_id = $2 AND entity_type = 'warehouse'
                FOR UPDATE
                """,
                job_id,
                tenant_id,
            )
            if not job:
                raise HTTPException(status_code=404, detail="Import job not found")
            if job["status"] == "committed":
                raise HTTPException(status_code=409, detail="Job already committed")
            if job["status"] != "dry_run" or not job["dry_run_report"]:
                raise HTTPException(status_code=409, detail="Run dry-run before commit")

            report = job["dry_run_report"]
            if isinstance(report, str):
                report = json.loads(report)
            if report.get("quota_exceeded"):
                raise HTTPException(
                    status_code=429,
                    detail="Plan quota exceeded for this import; re-run dry-run after reducing rows",
                )
            valid_rows = report.get("valid") or []
            if not valid_rows:
                raise HTTPException(status_code=422, detail="No valid rows to commit")

            committed = []
            failed = []
            for item in valid_rows:
                try:
                    async with conn.transaction():
                        purchase_units = []
                        if item["is_resale"]:
                            purchase_units = [
                                PurchaseUnitInput(purchase_unit="und", is_default=True)
                            ]
                        ing = await create_tenant_ingredient(
                            conn,
                            tenant_id,
                            TenantIngredientCreate(
                                name=item["name"],
                                unit=item["unit"],
                                type=item["type"],
                                category=item.get("category"),
                                costo_unitario=item.get("costo_unitario"),
                                is_resale=item["is_resale"],
                                unit_weight_gr=item.get("unit_weight_gr"),
                                unit_weight_unit=item.get("unit_weight_unit") or "gr",
                                purchase_units=purchase_units,
                            ),
                        )
                        product_id = None
                        if item.get("will_create_product"):
                            cat_id = UUID(item["menu_category_id"])
                            product_id = await create_resale_product_for_ingredient(
                                conn,
                                tenant_id,
                                ingredient_id=UUID(ing["id"]),
                                name=item["name"],
                                price=Decimal(str(item["price"])),
                                category_id=cat_id,
                                is_available=True,
                            )
                        committed.append(
                            {
                                "row": item["row"],
                                "name": item["name"],
                                "ingredient_id": ing["id"],
                                "product_id": str(product_id) if product_id else None,
                                "needs_product": bool(item.get("needs_product")),
                            }
                        )
                except HTTPException as exc:
                    failed.append(
                        {
                            "row": item["row"],
                            "name": item.get("name"),
                            "error": exc.detail,
                        }
                    )
                except Exception as exc:
                    logger.exception("warehouse import commit row failed")
                    failed.append(
                        {
                            "row": item["row"],
                            "name": item.get("name"),
                            "error": str(exc),
                        }
                    )

            commit_report = {"committed": committed, "failed": failed}
            status = "committed" if committed else "failed"
            await conn.execute(
                """
                UPDATE menu_import_jobs
                SET status = $2,
                    row_committed = $3,
                    commit_report = $4::jsonb,
                    updated_at = NOW(),
                    error_message = $5
                WHERE id = $1
                """,
                job_id,
                status,
                len(committed),
                json.dumps(commit_report),
                None if committed else "No rows committed",
            )

    return {
        "success": True,
        "data": {
            "id": str(job_id),
            "status": status,
            "row_committed": len(committed),
            "report": commit_report,
        },
    }


async def list_import_jobs(request: Request, limit: int = 20) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, entity_type, status, file_name, row_total, row_valid, row_invalid,
                   row_committed, created_at, updated_at
            FROM menu_import_jobs
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            tenant_id,
            limit,
        )
    return {
        "success": True,
        "data": [
            {
                "id": str(r["id"]),
                "entity_type": r["entity_type"],
                "status": r["status"],
                "file_name": r["file_name"],
                "row_total": r["row_total"],
                "row_valid": r["row_valid"],
                "row_invalid": r["row_invalid"],
                "row_committed": r["row_committed"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ],
    }


async def get_import_job(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            "SELECT * FROM menu_import_jobs WHERE id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    download_url = None
    if job["s3_key"]:
        s3 = AWSS3Service()
        download_url = await s3.get_presigned_url(job["s3_key"], expiration=3600)

    dry = job["dry_run_report"]
    commit = job["commit_report"]
    if isinstance(dry, str):
        dry = json.loads(dry)
    if isinstance(commit, str):
        commit = json.loads(commit)

    return {
        "success": True,
        "data": {
            "id": str(job["id"]),
            "entity_type": job["entity_type"],
            "status": job["status"],
            "file_name": job["file_name"],
            "row_total": job["row_total"],
            "row_valid": job["row_valid"],
            "row_invalid": job["row_invalid"],
            "row_committed": job["row_committed"],
            "dry_run_report": dry,
            "commit_report": commit,
            "download_url": download_url,
            "retention_days": RETENTION_DAYS,
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
            "updated_at": job["updated_at"].isoformat() if job["updated_at"] else None,
        },
    }


def template_streaming_response() -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(warehouse_csv_template_bytes()),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="waro-bodega-import-template.csv"'
        },
    )
