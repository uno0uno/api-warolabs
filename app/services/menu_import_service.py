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
import asyncpg

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.ingredient import PurchaseUnitInput, TenantIngredientCreate
from app.models.recipe_base import RecipeBaseIngredientCreate, RecipeBaseTypeCreate
from app.models.modifier import ModifierCreate, ModifierGroupCreate
from app.services.aws_s3_service import AWSS3Service
from app.services.billing_service import (
    check_plan_quota_growth,
    check_plan_quota_scoped,
    preview_plan_quota_growth,
)
from app.services.ingredient_purchase_units_service import resolve_to_base_unit
from app.services.ingredients_service import create_tenant_ingredient
from app.services.recipe_bases_service import create_recipe_base_on_conn
from app.services.modifiers_service import create_modifier_group_on_conn
from app.services import cost_resolution_service

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

RECIPE_BASES_TEMPLATE_HEADERS = [
    "recipe_name",
    "ingredient",
    "quantity",
    "unit",
    "notes",
    "description",
]

PRODUCTS_TEMPLATE_HEADERS = [
    "name",
    "price",
    "menu_category",
    "is_available",
    "recipe_bases",
    "finish_resale",
]

MODIFIERS_TEMPLATE_HEADERS = [
    "group_name",
    "option_name",
    "price",
    "min_qty",
    "max_qty",
    "is_required",
    "option_type",
    "ingredient",
    "ingredient_quantity",
    "ingredient_unit",
    "recipe_base",
    "recipe_base_quantity",
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


def recipe_bases_csv_template_bytes() -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RECIPE_BASES_TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow(
        {
            "recipe_name": "Salsa tomate",
            "ingredient": "Tomate",
            "quantity": "0.5",
            "unit": "kg",
            "notes": "",
            "description": "Base para pastas",
        }
    )
    writer.writerow(
        {
            "recipe_name": "Salsa tomate",
            "ingredient": "Cebolla",
            "quantity": "0.1",
            "unit": "kg",
            "notes": "picada",
            "description": "Base para pastas",
        }
    )
    return buf.getvalue().encode("utf-8-sig")


def products_csv_template_bytes() -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PRODUCTS_TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow(
        {
            "name": "Pasta bolognesa",
            "price": "18000",
            "menu_category": "Platos",
            "is_available": "true",
            "recipe_bases": "Salsa tomate",
            "finish_resale": "false",
        }
    )
    writer.writerow(
        {
            "name": "Coca-Cola 350ml",
            "price": "3500",
            "menu_category": "Bebidas",
            "is_available": "true",
            "recipe_bases": "",
            "finish_resale": "true",
        }
    )
    return buf.getvalue().encode("utf-8-sig")


def modifiers_csv_template_bytes() -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=MODIFIERS_TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow(
        {
            "group_name": "Punto de cocción",
            "option_name": "Término medio",
            "price": "0",
            "min_qty": "0",
            "max_qty": "1",
            "is_required": "false",
            "option_type": "NONE",
            "ingredient": "",
            "ingredient_quantity": "",
            "ingredient_unit": "",
            "recipe_base": "",
            "recipe_base_quantity": "",
        }
    )
    writer.writerow(
        {
            "group_name": "Punto de cocción",
            "option_name": "Bien asado",
            "price": "0",
            "min_qty": "0",
            "max_qty": "1",
            "is_required": "false",
            "option_type": "NONE",
            "ingredient": "",
            "ingredient_quantity": "",
            "ingredient_unit": "",
            "recipe_base": "",
            "recipe_base_quantity": "",
        }
    )
    writer.writerow(
        {
            "group_name": "Extras",
            "option_name": "Queso extra",
            "price": "2000",
            "min_qty": "0",
            "max_qty": "3",
            "is_required": "false",
            "option_type": "INGREDIENT",
            "ingredient": "Queso mozzarella",
            "ingredient_quantity": "0.05",
            "ingredient_unit": "kg",
            "recipe_base": "",
            "recipe_base_quantity": "",
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


async def _find_ingredient_by_name(conn, tenant_id: UUID, name: str) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        """
        SELECT id, name, unit
        FROM ingredients
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
          AND COALESCE(is_active, true) = true
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return dict(row) if row else None


async def _recipe_base_name_exists(conn, tenant_id: UUID, name: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM product_base_types
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return bool(row)


async def _unit_resolves_via_create_path(
    conn, ingredient_id: UUID, quantity: float, unit: str
) -> Tuple[bool, Optional[str]]:
    """
    Use resolve_to_base_unit (same as create). Fail when unit differs from base
    and the resolver could not convert (silent fallback would leave unit unchanged).
    """
    unit = (unit or "").strip()
    if not unit:
        return False, "unit is required"
    ing = await conn.fetchrow("SELECT unit FROM ingredients WHERE id = $1", ingredient_id)
    if not ing:
        return False, "ingredient not found"
    base_unit = ing["unit"]
    _qty, out_unit = await resolve_to_base_unit(conn, ingredient_id, quantity, unit)
    if unit != base_unit and out_unit == unit:
        return False, f"unit '{unit}' not resolvable for ingredient (base '{base_unit}')"
    return True, None


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


def parse_recipe_bases_csv(content: bytes) -> List[Dict[str, str]]:
    return parse_warehouse_csv(content)


def validate_recipe_base_line(row: Dict[str, Any], row_num: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    recipe_name = (row.get("recipe_name") or "").strip()
    if not recipe_name:
        return None, {"row": row_num, "field": "recipe_name", "error": "recipe_name is required"}

    ingredient = (row.get("ingredient") or row.get("ingredient_name") or "").strip()
    if not ingredient:
        return None, {"row": row_num, "field": "ingredient", "error": "ingredient is required"}

    qty = _parse_float(row.get("quantity"))
    if qty is None or qty <= 0:
        return None, {"row": row_num, "field": "quantity", "error": "quantity must be a positive number"}

    unit = (row.get("unit") or "").strip()
    if not unit:
        return None, {"row": row_num, "field": "unit", "error": "unit is required"}

    notes = (row.get("notes") or "").strip() or None
    description = (row.get("description") or "").strip() or None

    return {
        "row": row_num,
        "recipe_name": recipe_name,
        "ingredient": ingredient,
        "quantity": qty,
        "unit": unit,
        "notes": notes,
        "description": description,
    }, None


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


async def upload_recipe_bases_import(
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
        parse_recipe_bases_csv(raw)
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
            VALUES ($1, $2, 'recipe_bases', 'uploaded', $3, $4, $5, $6)
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
            "entity_type": "recipe_bases",
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        },
    }


async def dry_run_recipe_bases_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            """
            SELECT * FROM menu_import_jobs
            WHERE id = $1 AND tenant_id = $2 AND entity_type = 'recipe_bases'
            """,
            job_id,
            tenant_id,
        )
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found")
        if job["status"] == "committed":
            raise HTTPException(status_code=409, detail="Job already committed")

        raw = await _load_job_bytes(conn, dict(job))
        rows = parse_recipe_bases_csv(raw)

        errors: List[Dict[str, Any]] = []
        # recipe_name_lower -> accumulating group
        groups: Dict[str, Dict[str, Any]] = {}
        group_order: List[str] = []
        failed_groups: set = set()

        for idx, row in enumerate(rows, start=2):
            ok, err = validate_recipe_base_line(row, idx)
            if err:
                errors.append(err)
                # if we know recipe_name, mark group failed
                rn = (row.get("recipe_name") or "").strip().lower()
                if rn:
                    failed_groups.add(rn)
                continue
            assert ok is not None
            key = ok["recipe_name"].lower()
            if key in failed_groups:
                errors.append(
                    {
                        "row": idx,
                        "field": "recipe_name",
                        "error": "recipe skipped due to earlier line errors",
                    }
                )
                continue

            ing = await _find_ingredient_by_name(conn, tenant_id, ok["ingredient"])
            if not ing:
                errors.append(
                    {
                        "row": idx,
                        "field": "ingredient",
                        "error": f"ingredient '{ok['ingredient']}' not found in bodega",
                    }
                )
                failed_groups.add(key)
                groups.pop(key, None)
                continue

            resolvable, unit_err = await _unit_resolves_via_create_path(
                conn, ing["id"], ok["quantity"], ok["unit"]
            )
            if not resolvable:
                errors.append({"row": idx, "field": "unit", "error": unit_err})
                failed_groups.add(key)
                groups.pop(key, None)
                continue

            if key not in groups:
                if await _recipe_base_name_exists(conn, tenant_id, ok["recipe_name"]):
                    errors.append(
                        {
                            "row": idx,
                            "field": "recipe_name",
                            "error": "recipe already exists",
                        }
                    )
                    failed_groups.add(key)
                    continue
                groups[key] = {
                    "recipe_name": ok["recipe_name"],
                    "description": ok.get("description"),
                    "ingredients": [],
                    "rows": [],
                    "_seen_ingredient_ids": set(),
                }
                group_order.append(key)

            group = groups[key]
            if ok.get("description") and not group.get("description"):
                group["description"] = ok["description"]
            iid = str(ing["id"])
            if iid in group["_seen_ingredient_ids"]:
                errors.append(
                    {
                        "row": idx,
                        "field": "ingredient",
                        "error": "duplicate ingredient in recipe",
                    }
                )
                failed_groups.add(key)
                groups.pop(key, None)
                continue
            group["_seen_ingredient_ids"].add(iid)
            group["ingredients"].append(
                {
                    "ingredient_id": iid,
                    "ingredient_name": ing["name"],
                    "base_quantity": ok["quantity"],
                    "unit": ok["unit"],
                    "notes": ok.get("notes"),
                    "row": ok["row"],
                }
            )
            group["rows"].append(ok["row"])

        # Drop any groups that later failed
        for key in list(groups.keys()):
            if key in failed_groups:
                groups.pop(key, None)

        valid = []
        for key in group_order:
            if key not in groups:
                continue
            g = groups[key]
            if not g["ingredients"]:
                continue
            g.pop("_seen_ingredient_ids", None)
            valid.append(g)

        # Also mark duplicate recipe names that appeared after first valid start — already handled via failed_groups

        quota_hits: List[Dict[str, Any]] = []
        hit = await preview_plan_quota_growth(
            conn, tenant_id, "recipe_bases", additional=len(valid)
        )
        if hit:
            quota_hits.append(hit)
            errors.append(
                {
                    "row": None,
                    "field": "quota",
                    "error": (
                        f"recipe_bases: projected {hit['projected']} exceeds limit "
                        f"{hit['limit']} (used {hit['used']} + {hit['additional']})"
                    ),
                }
            )

        report = {
            "valid": valid,
            "errors": errors,
            "recipe_count": len(valid),
            "line_total": len(rows),
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


async def commit_recipe_bases_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                SELECT * FROM menu_import_jobs
                WHERE id = $1 AND tenant_id = $2 AND entity_type = 'recipe_bases'
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
            valid_recipes = report.get("valid") or []
            if not valid_recipes:
                raise HTTPException(status_code=422, detail="No valid recipes to commit")

            committed = []
            failed = []
            for item in valid_recipes:
                try:
                    async with conn.transaction():
                        ingredients = [
                            RecipeBaseIngredientCreate(
                                ingredient_id=UUID(line["ingredient_id"]),
                                base_quantity=float(line["base_quantity"]),
                                unit=line["unit"],
                                is_required=True,
                                notes=line.get("notes"),
                            )
                            for line in item["ingredients"]
                        ]
                        recipe_id = await create_recipe_base_on_conn(
                            conn,
                            tenant_id,
                            RecipeBaseTypeCreate(
                                name=item["recipe_name"],
                                description=item.get("description"),
                                is_active=True,
                                ingredients=ingredients,
                            ),
                            user_id=user_id,
                            record_history=True,
                        )
                        committed.append(
                            {
                                "recipe_name": item["recipe_name"],
                                "recipe_base_id": str(recipe_id),
                                "ingredient_count": len(ingredients),
                            }
                        )
                except HTTPException as exc:
                    failed.append(
                        {
                            "recipe_name": item.get("recipe_name"),
                            "error": exc.detail,
                        }
                    )
                except Exception as exc:
                    logger.exception("recipe_bases import commit failed")
                    failed.append(
                        {
                            "recipe_name": item.get("recipe_name"),
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
                None if committed else "No recipes committed",
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


def parse_products_csv(content: bytes) -> List[Dict[str, str]]:
    return parse_warehouse_csv(content)


def validate_product_row(
    row: Dict[str, Any], row_num: int
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    name = (row.get("name") or "").strip()
    if not name:
        return None, {"row": row_num, "field": "name", "error": "name is required"}

    finish_resale = _truthy(row.get("finish_resale"))
    price = _parse_decimal(row.get("price"))
    if price is None or price <= 0:
        return None, {"row": row_num, "field": "price", "error": "price must be a positive number"}

    menu_category = (row.get("menu_category") or "").strip()
    if not menu_category:
        return None, {"row": row_num, "field": "menu_category", "error": "menu_category is required"}

    recipe_bases_raw = (row.get("recipe_bases") or "").strip()
    recipe_base_names = [
        p.strip() for p in recipe_bases_raw.replace("|", ";").split(";") if p.strip()
    ]
    if finish_resale and recipe_base_names:
        return None, {
            "row": row_num,
            "field": "recipe_bases",
            "error": "finish_resale cannot be combined with recipe_bases",
        }

    is_available = True
    if row.get("is_available") is not None and str(row.get("is_available")).strip() != "":
        is_available = _truthy(row.get("is_available"))

    return {
        "row": row_num,
        "name": name,
        "price": price,
        "menu_category": menu_category,
        "is_available": is_available,
        "recipe_base_names": recipe_base_names,
        "finish_resale": finish_resale,
    }, None


async def _product_name_exists(conn, tenant_id: UUID, name: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM product
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return bool(row)


async def _find_recipe_base_by_name(conn, tenant_id: UUID, name: str) -> Optional[UUID]:
    row = await conn.fetchrow(
        """
        SELECT id FROM product_base_types
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return row["id"] if row else None


async def _find_incomplete_resale_ingredient(
    conn, tenant_id: UUID, name: str
) -> Optional[Dict[str, Any]]:
    """is_resale ingredient with no linked resale product."""
    row = await conn.fetchrow(
        """
        SELECT i.id, i.name, i.unit, i.is_resale
        FROM ingredients i
        WHERE i.tenant_id = $1
          AND LOWER(TRIM(i.name)) = LOWER(TRIM($2))
          AND COALESCE(i.is_active, true) = true
          AND COALESCE(i.is_resale, false) = true
          AND NOT EXISTS (
            SELECT 1
            FROM product_recipes pr
            INNER JOIN product p ON p.id = pr.product_id AND p.tenant_id = i.tenant_id
            WHERE pr.ingredient_id = i.id AND COALESCE(p.is_resale, false) = true
          )
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return dict(row) if row else None


async def create_menu_product_on_conn(
    conn,
    tenant_id: UUID,
    *,
    name: str,
    price: Decimal,
    category_id: UUID,
    is_available: bool = True,
    recipe_base_ids: Optional[List[UUID]] = None,
) -> UUID:
    """Menu product create subset for CSV (no auto_resale; use create_resale_product_for_ingredient)."""
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
            true, $4, true, false,
            false, false, false, true,
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
    for rb_id in recipe_base_ids or []:
        # v1: qty=1 per base; CSV has no qty column yet
        await conn.execute(
            """
            INSERT INTO product_base_recipes (
                product_id, product_base_type_id, tenant_id, quantity
            )
            VALUES ($1, $2, $3, 1)
            """,
            product_id,
            rb_id,
            tenant_id,
        )
    tracks = bool(recipe_base_ids)
    await cost_resolution_service.persist_product_costo_calculado(
        product_id,
        tenant_id,
        conn,
        tracks_inventory=tracks,
    )
    return product_id


async def upload_products_import(request: Request, file: UploadFile) -> Dict[str, Any]:
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
        parse_products_csv(raw)
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
            VALUES ($1, $2, 'products', 'uploaded', $3, $4, $5, $6)
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
            "entity_type": "products",
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        },
    }


async def dry_run_products_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            """
            SELECT * FROM menu_import_jobs
            WHERE id = $1 AND tenant_id = $2 AND entity_type = 'products'
            """,
            job_id,
            tenant_id,
        )
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found")
        if job["status"] == "committed":
            raise HTTPException(status_code=409, detail="Job already committed")

        raw = await _load_job_bytes(conn, dict(job))
        rows = parse_products_csv(raw)
        valid: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        seen_names = set()

        for idx, row in enumerate(rows, start=2):
            ok, err = validate_product_row(row, idx)
            if err:
                errors.append(err)
                continue
            assert ok is not None
            key = ok["name"].lower()
            if key in seen_names:
                errors.append({"row": idx, "field": "name", "error": "duplicate name in file"})
                continue
            seen_names.add(key)

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

            if ok["finish_resale"]:
                ing = await _find_incomplete_resale_ingredient(conn, tenant_id, ok["name"])
                if not ing:
                    errors.append(
                        {
                            "row": idx,
                            "field": "name",
                            "error": (
                                f"no incomplete resale ingredient named '{ok['name']}' "
                                "(need is_resale und without linked product)"
                            ),
                        }
                    )
                    continue
                if await _product_name_exists(conn, tenant_id, ok["name"]):
                    errors.append(
                        {"row": idx, "field": "name", "error": "product already exists"}
                    )
                    continue
                ok["ingredient_id"] = str(ing["id"])
            else:
                if await _product_name_exists(conn, tenant_id, ok["name"]):
                    errors.append(
                        {"row": idx, "field": "name", "error": "product already exists"}
                    )
                    continue
                rb_ids = []
                rb_fail = False
                for rb_name in ok["recipe_base_names"]:
                    rb_id = await _find_recipe_base_by_name(conn, tenant_id, rb_name)
                    if not rb_id:
                        errors.append(
                            {
                                "row": idx,
                                "field": "recipe_bases",
                                "error": f"recipe base '{rb_name}' not found",
                            }
                        )
                        rb_fail = True
                        break
                    rb_ids.append(str(rb_id))
                if rb_fail:
                    continue
                ok["recipe_base_ids"] = rb_ids

            # serialize Decimal
            ok["price"] = str(ok["price"])
            valid.append(ok)

        create_count = sum(1 for v in valid if not v.get("finish_resale"))
        finish_count = sum(1 for v in valid if v.get("finish_resale"))
        quota_hits: List[Dict[str, Any]] = []
        hit = await preview_plan_quota_growth(
            conn, tenant_id, "menu_products", additional=len(valid)
        )
        if hit:
            quota_hits.append(hit)
            errors.append(
                {
                    "row": None,
                    "field": "quota",
                    "error": (
                        f"menu_products: projected {hit['projected']} exceeds limit "
                        f"{hit['limit']} (used {hit['used']} + {hit['additional']})"
                    ),
                }
            )

        report = {
            "valid": valid,
            "errors": errors,
            "create_count": create_count,
            "finish_resale_count": finish_count,
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


async def commit_products_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                SELECT * FROM menu_import_jobs
                WHERE id = $1 AND tenant_id = $2 AND entity_type = 'products'
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
                        price = Decimal(str(item["price"]))
                        cat_id = UUID(item["menu_category_id"])
                        if item.get("finish_resale"):
                            product_id = await create_resale_product_for_ingredient(
                                conn,
                                tenant_id,
                                ingredient_id=UUID(item["ingredient_id"]),
                                name=item["name"],
                                price=price,
                                category_id=cat_id,
                                is_available=bool(item.get("is_available", True)),
                            )
                            committed.append(
                                {
                                    "row": item["row"],
                                    "name": item["name"],
                                    "product_id": str(product_id),
                                    "finish_resale": True,
                                }
                            )
                        else:
                            rb_ids = [UUID(x) for x in (item.get("recipe_base_ids") or [])]
                            product_id = await create_menu_product_on_conn(
                                conn,
                                tenant_id,
                                name=item["name"],
                                price=price,
                                category_id=cat_id,
                                is_available=bool(item.get("is_available", True)),
                                recipe_base_ids=rb_ids,
                            )
                            committed.append(
                                {
                                    "row": item["row"],
                                    "name": item["name"],
                                    "product_id": str(product_id),
                                    "finish_resale": False,
                                }
                            )
                except HTTPException as exc:
                    failed.append(
                        {"row": item.get("row"), "name": item.get("name"), "error": exc.detail}
                    )
                except asyncpg.UniqueViolationError:
                    failed.append(
                        {
                            "row": item.get("row"),
                            "name": item.get("name"),
                            "error": "product name already exists",
                        }
                    )
                except Exception as exc:
                    logger.exception("products import commit row failed")
                    failed.append(
                        {"row": item.get("row"), "name": item.get("name"), "error": str(exc)}
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


async def list_incomplete_resale_ingredients(request: Request) -> Dict[str, Any]:
    """Resale warehouse articles waiting for sell fields / linked product (Decision A)."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT i.id, i.name, i.unit
            FROM ingredients i
            WHERE i.tenant_id = $1
              AND COALESCE(i.is_active, true) = true
              AND COALESCE(i.is_resale, false) = true
              AND NOT EXISTS (
                SELECT 1
                FROM product_recipes pr
                INNER JOIN product p ON p.id = pr.product_id AND p.tenant_id = i.tenant_id
                WHERE pr.ingredient_id = i.id AND COALESCE(p.is_resale, false) = true
              )
            ORDER BY i.name ASC
            LIMIT 500
            """,
            tenant_id,
        )
    return {
        "success": True,
        "data": [
            {"id": str(r["id"]), "name": r["name"], "unit": r["unit"], "needs_product": True}
            for r in rows
        ],
    }


def parse_modifiers_csv(content: bytes) -> List[Dict[str, str]]:
    return parse_warehouse_csv(content)


def validate_modifier_line(
    row: Dict[str, Any], row_num: int
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    group_name = (row.get("group_name") or "").strip()
    if not group_name:
        return None, {"row": row_num, "field": "group_name", "error": "group_name is required"}

    option_name = (row.get("option_name") or "").strip()
    if not option_name:
        return None, {"row": row_num, "field": "option_name", "error": "option_name is required"}

    price = _parse_decimal(row.get("price"))
    if price is None:
        price = Decimal("0")

    min_qty = 0
    max_qty = 1
    if str(row.get("min_qty") or "").strip() != "":
        try:
            min_qty = int(float(str(row.get("min_qty")).strip().replace(",", ".")))
        except ValueError:
            return None, {"row": row_num, "field": "min_qty", "error": "min_qty must be an integer"}
    if str(row.get("max_qty") or "").strip() != "":
        try:
            max_qty = int(float(str(row.get("max_qty")).strip().replace(",", ".")))
        except ValueError:
            return None, {"row": row_num, "field": "max_qty", "error": "max_qty must be an integer"}
    if max_qty < 1:
        return None, {"row": row_num, "field": "max_qty", "error": "max_qty must be >= 1"}
    if max_qty < min_qty:
        return None, {"row": row_num, "field": "max_qty", "error": "max_qty must be >= min_qty"}

    is_required = _truthy(row.get("is_required"))
    option_type = (row.get("option_type") or "NONE").strip().upper() or "NONE"
    if option_type not in {"NONE", "INGREDIENT", "RECIPE", "PRODUCT"}:
        return None, {"row": row_num, "field": "option_type", "error": "invalid option_type"}

    ingredient = (row.get("ingredient") or "").strip() or None
    recipe_base = (row.get("recipe_base") or "").strip() or None
    ing_qty = _parse_decimal(row.get("ingredient_quantity"))
    ing_unit = (row.get("ingredient_unit") or "").strip() or None
    rb_qty = _parse_decimal(row.get("recipe_base_quantity")) or Decimal("1")

    if option_type == "INGREDIENT" and not ingredient:
        return None, {"row": row_num, "field": "ingredient", "error": "ingredient required for INGREDIENT"}
    if option_type == "INGREDIENT" and (ing_qty is None or ing_qty <= 0):
        return None, {
            "row": row_num,
            "field": "ingredient_quantity",
            "error": "ingredient_quantity must be positive",
        }
    if option_type == "INGREDIENT" and not ing_unit:
        return None, {"row": row_num, "field": "ingredient_unit", "error": "ingredient_unit required"}
    if option_type == "RECIPE" and not recipe_base:
        return None, {"row": row_num, "field": "recipe_base", "error": "recipe_base required for RECIPE"}
    if option_type == "PRODUCT":
        return None, {
            "row": row_num,
            "field": "option_type",
            "error": "PRODUCT option_type not supported in CSV v1",
        }
    if option_type == "NONE" and (ingredient or recipe_base):
        return None, {
            "row": row_num,
            "field": "option_type",
            "error": "NONE option must not set ingredient/recipe_base",
        }

    return {
        "row": row_num,
        "group_name": group_name,
        "option_name": option_name,
        "price": price,
        "min_qty": min_qty,
        "max_qty": max_qty,
        "is_required": is_required,
        "option_type": option_type,
        "ingredient": ingredient,
        "ingredient_quantity": ing_qty,
        "ingredient_unit": ing_unit,
        "recipe_base": recipe_base,
        "recipe_base_quantity": rb_qty,
    }, None


async def _modifier_group_name_exists(conn, tenant_id: UUID, name: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM modifier_groups
        WHERE tenant_id = $1 AND LOWER(TRIM(name)) = LOWER(TRIM($2))
        LIMIT 1
        """,
        tenant_id,
        name,
    )
    return bool(row)


async def upload_modifiers_import(request: Request, file: UploadFile) -> Dict[str, Any]:
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
        parse_modifiers_csv(raw)
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
            VALUES ($1, $2, 'modifiers', 'uploaded', $3, $4, $5, $6)
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
            "entity_type": "modifiers",
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        },
    }


async def dry_run_modifiers_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            """
            SELECT * FROM menu_import_jobs
            WHERE id = $1 AND tenant_id = $2 AND entity_type = 'modifiers'
            """,
            job_id,
            tenant_id,
        )
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found")
        if job["status"] == "committed":
            raise HTTPException(status_code=409, detail="Job already committed")

        raw = await _load_job_bytes(conn, dict(job))
        rows = parse_modifiers_csv(raw)
        errors: List[Dict[str, Any]] = []
        groups: Dict[str, Dict[str, Any]] = {}
        group_order: List[str] = []
        failed_groups: set = set()

        for idx, row in enumerate(rows, start=2):
            ok, err = validate_modifier_line(row, idx)
            if err:
                errors.append(err)
                rn = (row.get("group_name") or "").strip().lower()
                if rn:
                    failed_groups.add(rn)
                continue
            assert ok is not None
            key = ok["group_name"].lower()
            if key in failed_groups:
                errors.append(
                    {
                        "row": idx,
                        "field": "group_name",
                        "error": "group skipped due to earlier line errors",
                    }
                )
                continue

            if key not in groups:
                if await _modifier_group_name_exists(conn, tenant_id, ok["group_name"]):
                    errors.append(
                        {"row": idx, "field": "group_name", "error": "modifier group already exists"}
                    )
                    failed_groups.add(key)
                    continue
                groups[key] = {
                    "group_name": ok["group_name"],
                    "min_qty": ok["min_qty"],
                    "max_qty": ok["max_qty"],
                    "is_required": ok["is_required"],
                    "options": [],
                    "_seen_options": set(),
                }
                group_order.append(key)

            group = groups[key]
            opt_key = ok["option_name"].lower()
            if opt_key in group["_seen_options"]:
                errors.append(
                    {"row": idx, "field": "option_name", "error": "duplicate option in group"}
                )
                failed_groups.add(key)
                groups.pop(key, None)
                continue

            option_payload: Dict[str, Any] = {
                "row": ok["row"],
                "name": ok["option_name"],
                "price": str(ok["price"]),
                "option_type": ok["option_type"],
            }

            if ok["option_type"] == "INGREDIENT":
                ing = await _find_ingredient_by_name(conn, tenant_id, ok["ingredient"])
                if not ing:
                    errors.append(
                        {
                            "row": idx,
                            "field": "ingredient",
                            "error": f"ingredient '{ok['ingredient']}' not found",
                        }
                    )
                    failed_groups.add(key)
                    groups.pop(key, None)
                    continue
                resolvable, unit_err = await _unit_resolves_via_create_path(
                    conn, ing["id"], float(ok["ingredient_quantity"]), ok["ingredient_unit"]
                )
                if not resolvable:
                    errors.append({"row": idx, "field": "ingredient_unit", "error": unit_err})
                    failed_groups.add(key)
                    groups.pop(key, None)
                    continue
                option_payload["ingredient_id"] = str(ing["id"])
                option_payload["ingredient_quantity"] = str(ok["ingredient_quantity"])
                option_payload["ingredient_unit"] = ok["ingredient_unit"]

            if ok["option_type"] == "RECIPE":
                rb_id = await _find_recipe_base_by_name(conn, tenant_id, ok["recipe_base"])
                if not rb_id:
                    errors.append(
                        {
                            "row": idx,
                            "field": "recipe_base",
                            "error": f"recipe base '{ok['recipe_base']}' not found",
                        }
                    )
                    failed_groups.add(key)
                    groups.pop(key, None)
                    continue
                option_payload["recipe_base_type_id"] = str(rb_id)
                option_payload["recipe_base_quantity"] = str(ok["recipe_base_quantity"])

            group["_seen_options"].add(opt_key)
            group["options"].append(option_payload)

        for key in list(groups.keys()):
            if key in failed_groups:
                groups.pop(key, None)

        valid = []
        for key in group_order:
            if key not in groups:
                continue
            g = groups[key]
            if not g["options"]:
                continue
            g.pop("_seen_options", None)
            valid.append(g)

        quota_hits: List[Dict[str, Any]] = []
        hit = await preview_plan_quota_growth(
            conn, tenant_id, "modifier_groups", additional=len(valid)
        )
        if hit:
            quota_hits.append(hit)
            errors.append(
                {
                    "row": None,
                    "field": "quota",
                    "error": (
                        f"modifier_groups: projected {hit['projected']} exceeds limit "
                        f"{hit['limit']} (used {hit['used']} + {hit['additional']})"
                    ),
                }
            )

        report = {
            "valid": valid,
            "errors": errors,
            "group_count": len(valid),
            "line_total": len(rows),
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


async def commit_modifiers_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                SELECT * FROM menu_import_jobs
                WHERE id = $1 AND tenant_id = $2 AND entity_type = 'modifiers'
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
            valid_groups = report.get("valid") or []
            if not valid_groups:
                raise HTTPException(status_code=422, detail="No valid groups to commit")

            committed = []
            failed = []
            for item in valid_groups:
                try:
                    async with conn.transaction():
                        modifiers = []
                        for opt in item["options"]:
                            modifiers.append(
                                ModifierCreate(
                                    name=opt["name"],
                                    price=Decimal(str(opt["price"])),
                                    option_type=opt["option_type"],
                                    ingredient_id=(
                                        UUID(opt["ingredient_id"])
                                        if opt.get("ingredient_id")
                                        else None
                                    ),
                                    ingredient_quantity=(
                                        Decimal(str(opt["ingredient_quantity"]))
                                        if opt.get("ingredient_quantity") is not None
                                        else None
                                    ),
                                    ingredient_unit=opt.get("ingredient_unit"),
                                    recipe_base_type_id=(
                                        UUID(opt["recipe_base_type_id"])
                                        if opt.get("recipe_base_type_id")
                                        else None
                                    ),
                                    recipe_base_quantity=Decimal(
                                        str(opt.get("recipe_base_quantity") or "1")
                                    ),
                                )
                            )
                        group_id = await create_modifier_group_on_conn(
                            conn,
                            tenant_id,
                            ModifierGroupCreate(
                                name=item["group_name"],
                                min_qty=int(item.get("min_qty") or 0),
                                max_qty=int(item.get("max_qty") or 1),
                                is_required=bool(item.get("is_required")),
                                product_ids=[],
                                modifiers=modifiers,
                            ),
                            user_id=user_id,
                            record_history=True,
                        )
                        committed.append(
                            {
                                "group_name": item["group_name"],
                                "modifier_group_id": str(group_id),
                                "option_count": len(modifiers),
                            }
                        )
                except HTTPException as exc:
                    failed.append(
                        {"group_name": item.get("group_name"), "error": exc.detail}
                    )
                except ValueError as exc:
                    failed.append(
                        {"group_name": item.get("group_name"), "error": str(exc)}
                    )
                except Exception as exc:
                    logger.exception("modifiers import commit failed")
                    failed.append(
                        {"group_name": item.get("group_name"), "error": str(exc)}
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
                None if committed else "No groups committed",
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


async def dry_run_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")
    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            "SELECT entity_type FROM menu_import_jobs WHERE id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    entity = job["entity_type"]
    if entity == "warehouse":
        return await dry_run_warehouse_import(request, job_id)
    if entity == "recipe_bases":
        return await dry_run_recipe_bases_import(request, job_id)
    if entity == "products":
        return await dry_run_products_import(request, job_id)
    if entity == "modifiers":
        return await dry_run_modifiers_import(request, job_id)
    raise HTTPException(status_code=422, detail=f"Unsupported entity_type: {entity}")


async def commit_import(request: Request, job_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")
    async with get_db_connection() as conn:
        job = await conn.fetchrow(
            "SELECT entity_type FROM menu_import_jobs WHERE id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    entity = job["entity_type"]
    if entity == "warehouse":
        return await commit_warehouse_import(request, job_id)
    if entity == "recipe_bases":
        return await commit_recipe_bases_import(request, job_id)
    if entity == "products":
        return await commit_products_import(request, job_id)
    if entity == "modifiers":
        return await commit_modifiers_import(request, job_id)
    raise HTTPException(status_code=422, detail=f"Unsupported entity_type: {entity}")


async def list_import_jobs(
    request: Request,
    limit: int = 20,
    entity_type: Optional[str] = None,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant session required")

    async with get_db_connection() as conn:
        if entity_type:
            rows = await conn.fetch(
                """
                SELECT id, entity_type, status, file_name, row_total, row_valid, row_invalid,
                       row_committed, created_at, updated_at
                FROM menu_import_jobs
                WHERE tenant_id = $1 AND entity_type = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                tenant_id,
                entity_type,
                limit,
            )
        else:
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


def template_streaming_response(entity: str = "warehouse") -> StreamingResponse:
    if entity == "recipe_bases":
        payload = recipe_bases_csv_template_bytes()
        filename = "waro-recipe-bases-import-template.csv"
    elif entity == "products":
        payload = products_csv_template_bytes()
        filename = "waro-products-import-template.csv"
    elif entity == "modifiers":
        payload = modifiers_csv_template_bytes()
        filename = "waro-modifiers-import-template.csv"
    else:
        payload = warehouse_csv_template_bytes()
        filename = "waro-bodega-import-template.csv"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
