from typing import Optional, List, Tuple
from uuid import UUID
from decimal import Decimal
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.product import (
    Product, ProductCreate, ProductUpdate, ProductsListResponse,
    ProductResponse, ProductStats, RecipeBaseLink
)
from app.services import menu_history_service
from app.services import cost_resolution_service
from app.services.aws_s3_service import AWSS3Service
from app.services.ingredient_purchase_units_service import resolve_to_base_unit
from app.services.open_priced_service import assert_single_open_priced_per_tenant
from app.services.ingredients_service import create_tenant_ingredient
from app.models.ingredient import TenantIngredientCreate, PurchaseUnitInput
import asyncpg
import logging

logger = logging.getLogger(__name__)

_DUPLICATE_PRODUCT_NAME_DETAIL = "Ya existe un producto con ese nombre en tu menú"

# Dual margins: real (costo_calculado) + operativo (costo_percibido). Outer SELECT only.
_PRODUCT_MARGIN_OUTER_SQL = """
    CASE
        WHEN costo_calculado > 0 AND costo_calculado IS NOT NULL
        THEN ((price - costo_calculado) / costo_calculado * 100)
        ELSE NULL
    END as margen_real_pct,
    CASE
        WHEN costo_calculado IS NOT NULL
        THEN (price - costo_calculado)
        ELSE NULL
    END as margen_real_valor,
    CASE
        WHEN costo_percibido > 0 AND costo_percibido IS NOT NULL
        THEN ((price - costo_percibido) / costo_percibido * 100)
        ELSE NULL
    END as margen_operativo_pct,
    CASE
        WHEN costo_percibido IS NOT NULL
        THEN (price - costo_percibido)
        ELSE NULL
    END as margen_operativo_valor,
    CASE
        WHEN costo_calculado > 0 AND costo_calculado IS NOT NULL
        THEN ((price - costo_calculado) / costo_calculado * 100)
        ELSE NULL
    END as margen_porcentaje,
    CASE
        WHEN costo_calculado IS NOT NULL
        THEN (price - costo_calculado)
        ELSE NULL
    END as margen_valor
"""

_PRODUCT_SEARCH_FIELDS = frozenset({"name", "description", "kitchen_name"})

_PRODUCT_SORT_SQL = {
    "created_at_desc": "created_at DESC",
    "created_at_asc": "created_at ASC",
    "name_asc": "name ASC",
    "name_desc": "name DESC",
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "margin_asc": "margen_valor ASC NULLS LAST",
    "margin_desc": "margen_valor DESC NULLS LAST",
}

_HAS_RECIPE_SQL = """
    (
        EXISTS (SELECT 1 FROM product_recipes pr WHERE pr.product_id = p.id)
        OR EXISTS (SELECT 1 FROM product_base_recipes pbr WHERE pbr.product_id = p.id)
        OR p.product_base_type_id IS NOT NULL
    )
"""


def _resolve_products_sort(sort: Optional[str]) -> str:
    key = (sort or "created_at_desc").strip().lower()
    order = _PRODUCT_SORT_SQL.get(key)
    if not order:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid sort '{sort}'. Allowed: {', '.join(sorted(_PRODUCT_SORT_SQL))}",
        )
    return order


def _normalize_recipe_bases(
    recipe_bases: Optional[List[RecipeBaseLink]],
    recipe_base_ids: Optional[List[UUID]],
) -> List[Tuple[UUID, Decimal]]:
    """Normalize the legacy `recipe_base_ids` and the new `recipe_bases` shapes
    into a single deduplicated list of (recipe_base_id, quantity) tuples.

    Prefers `recipe_bases` (with explicit per-link quantity) when non-empty.
    Falls back to `recipe_base_ids` (each treated as quantity=1).
    Deduplication keeps the FIRST occurrence per recipe_base_id.
    """
    seen: dict = {}
    if recipe_bases:
        for link in recipe_bases:
            if link.recipe_base_id not in seen:
                seen[link.recipe_base_id] = Decimal(link.quantity)
        return [(rid, qty) for rid, qty in seen.items()]
    if recipe_base_ids:
        for rid in recipe_base_ids:
            if rid not in seen:
                seen[rid] = Decimal("1")
        return [(rid, qty) for rid, qty in seen.items()]
    return []


async def _resolve_resale_ingredient_category(
    conn,
    tenant_id: UUID,
    category_id: UUID,
    override: Optional[str],
) -> str:
    """Category string for auto-created resale ingredients."""
    if override and override.strip():
        return override.strip()
    row = await conn.fetchrow(
        "SELECT name FROM categories WHERE id = $1 AND tenant_id = $2",
        category_id,
        tenant_id,
    )
    if row and row.get("name"):
        return row["name"]
    return "Reventa"


async def create_product_with_recipe(
    request: Request,
    product_data: ProductCreate
) -> ProductResponse:
    """
    Creates a product with its recipe in a single transaction.
    Steps:
    1. Insert product
    2. Insert recipe ingredients
    3. Calculate and update product cost
    4. Return complete product with recipe
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or product_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Products may be created without recipe — those skip inventory tracking.
        # See app/models/product.py ProductCreate docstring.
        has_ingredients = product_data.ingredients and len(product_data.ingredients) > 0
        normalized_bases = _normalize_recipe_bases(
            product_data.recipe_bases,
            product_data.recipe_base_ids,
        )
        has_recipe_bases = len(normalized_bases) > 0
        auto_resale = product_data.auto_resale_ingredient
        tracks_inventory = has_ingredients or has_recipe_bases or auto_resale
        auto_resale_ingredient_id: Optional[UUID] = None

        async with get_db_connection() as conn:
            # Start transaction
            async with conn.transaction():
                if product_data.open_priced:
                    await assert_single_open_priced_per_tenant(conn, tenant_id)

                if auto_resale:
                    ingredient_category = await _resolve_resale_ingredient_category(
                        conn,
                        tenant_id,
                        product_data.category_id,
                        product_data.resale_ingredient_category,
                    )
                    costo_ingredient = None
                    if product_data.costo_percibido is not None:
                        costo_ingredient = float(product_data.costo_percibido)
                    ing_result = await create_tenant_ingredient(
                        conn,
                        tenant_id,
                        TenantIngredientCreate(
                            name=product_data.name.strip(),
                            unit="und",
                            type=product_data.resale_ingredient_type,
                            category=ingredient_category,
                            is_resale=True,
                            unit_weight_gr=product_data.resale_unit_weight_gr,
                            unit_weight_unit=product_data.resale_unit_weight_unit,
                            costo_unitario=costo_ingredient,
                            purchase_units=[
                                PurchaseUnitInput(purchase_unit="und", is_default=True),
                            ],
                        ),
                    )
                    auto_resale_ingredient_id = UUID(ing_result["id"])

                # 1. Insert product
                # NOTE: controla_stock is ALWAYS True - all products control inventory
                product_query = """
                    INSERT INTO product (
                        name, description, price, category_id, product_base_type_id, preparation_time,
                        controla_stock, is_available, is_available_online, is_available_table_qr,
                        is_combo, is_resale, open_priced, allow_modifiers,
                        tax_category, tenant_id, station_id, kitchen_name, image_url, costo_percibido
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
                    RETURNING id, created_at, updated_at
                """
                product_result = await conn.fetchrow(
                    product_query,
                    product_data.name,
                    product_data.description,
                    product_data.price,
                    product_data.category_id,
                    product_data.product_base_type_id,
                    product_data.preparation_time,
                    True,  # ALWAYS True - controla_stock is mandatory
                    product_data.is_available,
                    product_data.is_available_online,
                    product_data.is_available_table_qr,
                    product_data.is_combo,
                    product_data.is_resale,
                    product_data.open_priced,
                    product_data.allow_modifiers,
                    product_data.tax_category,
                    tenant_id,
                    product_data.station_id,
                    product_data.kitchen_name,
                    product_data.image_url,
                    product_data.costo_percibido,
                )

                product_id = product_result['id']

                # 2. Insert recipe base associations (with per-product quantity, Issue #517)
                if normalized_bases:
                    base_recipe_query = """
                        INSERT INTO product_base_recipes (
                            product_id, product_base_type_id, tenant_id, quantity
                        )
                        VALUES ($1, $2, $3, $4)
                    """
                    for recipe_base_id, base_qty in normalized_bases:
                        await conn.execute(
                            base_recipe_query,
                            product_id,
                            recipe_base_id,
                            tenant_id,
                            base_qty,
                        )

                # 3. Insert recipe ingredients
                recipe_query = """
                    INSERT INTO product_recipes (
                        product_id, ingredient_id, quantity, unit, tenant_id
                    )
                    VALUES ($1, $2, $3, $4, $5)
                """
                if auto_resale_ingredient_id:
                    base_qty, base_unit = await resolve_to_base_unit(
                        conn,
                        auto_resale_ingredient_id,
                        1,
                        "und",
                    )
                    await conn.execute(
                        recipe_query,
                        product_id,
                        auto_resale_ingredient_id,
                        base_qty,
                        base_unit,
                        tenant_id,
                    )
                elif product_data.ingredients:
                    for ingredient in product_data.ingredients:
                        base_qty, base_unit = await resolve_to_base_unit(
                            conn,
                            ingredient.ingredient_id,
                            ingredient.quantity,
                            ingredient.unit
                        )
                        await conn.execute(
                            recipe_query,
                            product_id,
                            ingredient.ingredient_id,
                            base_qty,
                            base_unit,
                            tenant_id
                        )

                # 4. Unified real cost (direct recipes + base recipes)
                await cost_resolution_service.persist_product_costo_calculado(
                    product_id,
                    tenant_id,
                    conn,
                    tracks_inventory=tracks_inventory,
                )

                # 5. Registrar en historial
                user_id = session_context.user_id if hasattr(session_context, 'user_id') else None
                product_snapshot = await menu_history_service.get_product_snapshot(conn, product_id, tenant_id)
                if product_snapshot:
                    await menu_history_service.record_product_create(
                        conn, tenant_id, product_id, product_data.name,
                        product_snapshot, user_id
                    )

                # 6. Get complete product with recipe
                response = await get_product_by_id(request, product_id, conn)
                if auto_resale_ingredient_id:
                    response.data.resale_ingredient_id = auto_resale_ingredient_id
                return response

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=_DUPLICATE_PRODUCT_NAME_DETAIL)
    except Exception as e:
        logger.error(f"Error creating product: {str(e)}")
        raise APIError(f"Error creating product: {str(e)}", status_code=500)


async def get_product_by_id(
    request: Request,
    product_id: UUID,
    conn=None
) -> ProductResponse:
    """Get a single product with its recipe and calculated fields"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Use provided connection or create new one
        async def _fetch_product(connection):
            # Get product with category
            product_query = """
                SELECT
                    p.id,
                    p.name,
                    p.description,
                    p.price,
                    p.category_id,
                    c.name as category_name,
                    p.preparation_time,
                    p.controla_stock,
                    p.is_available,
                    p.is_available_online,
                    p.is_available_table_qr,
                    p.is_combo,
                    p.is_resale,
                    p.open_priced,
                    p.allow_modifiers,
                    p.tax_category,
                    p.costo_calculado,
                    p.costo_percibido,
                    p.precio_sugerido,
                    p.margen_objetivo,
                    p.tenant_id,
                    p.created_at,
                    p.updated_at,
                    p.station_id,
                    p.kitchen_name,
                    p.image_url,
                    ks.id as ks_id,
                    ks.name as station_name,
                    ks.color as station_color,
                    CASE
                        WHEN p.costo_calculado > 0
                        THEN ((p.price - p.costo_calculado) / p.costo_calculado * 100)
                        ELSE NULL
                    END as margen_real_pct,
                    CASE
                        WHEN p.costo_calculado IS NOT NULL
                        THEN (p.price - p.costo_calculado)
                        ELSE NULL
                    END as margen_real_valor,
                    CASE
                        WHEN p.costo_percibido > 0
                        THEN ((p.price - p.costo_percibido) / p.costo_percibido * 100)
                        ELSE NULL
                    END as margen_operativo_pct,
                    CASE
                        WHEN p.costo_percibido IS NOT NULL
                        THEN (p.price - p.costo_percibido)
                        ELSE NULL
                    END as margen_operativo_valor,
                    CASE
                        WHEN p.costo_calculado > 0
                        THEN ((p.price - p.costo_calculado) / p.costo_calculado * 100)
                        ELSE NULL
                    END as margen_porcentaje,
                    CASE
                        WHEN p.costo_calculado IS NOT NULL
                        THEN (p.price - p.costo_calculado)
                        ELSE NULL
                    END as margen_valor
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                LEFT JOIN tenant_category_stations tcs ON tcs.category_id = p.category_id AND tcs.tenant_id = p.tenant_id
                LEFT JOIN kitchen_stations ks ON ks.id = COALESCE(p.station_id, tcs.station_id)
                WHERE p.id = $1 AND p.tenant_id = $2
            """

            product_row = await connection.fetchrow(product_query, product_id, tenant_id)

            if not product_row:
                raise HTTPException(status_code=404, detail="Product not found")

            # Get recipe ingredients
            recipe_query = """
                SELECT
                    pr.id,
                    pr.product_id,
                    pr.ingredient_id,
                    pr.quantity,
                    pr.unit,
                    i.name as ingredient_name,
                    COALESCE(
                        (SELECT pi.unit_cost
                         FROM tenant_purchase_items pi
                         JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                         WHERE pi.ingredient_id = pr.ingredient_id
                           AND tp.tenant_id = $2
                           AND pi.unit_cost IS NOT NULL AND pi.unit_cost > 0
                         ORDER BY tp.purchase_date DESC LIMIT 1),
                        i.costo_unitario, 0
                    ) as ingredient_cost_per_unit,
                    pr.quantity * COALESCE(
                        (SELECT pi.unit_cost
                         FROM tenant_purchase_items pi
                         JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                         WHERE pi.ingredient_id = pr.ingredient_id
                           AND tp.tenant_id = $2
                           AND pi.unit_cost IS NOT NULL AND pi.unit_cost > 0
                         ORDER BY tp.purchase_date DESC LIMIT 1),
                        i.costo_unitario, 0
                    ) as total_cost
                FROM product_recipes pr
                JOIN ingredients i ON pr.ingredient_id = i.id
                WHERE pr.product_id = $1
                ORDER BY i.name
            """

            recipe_rows = await connection.fetch(recipe_query, product_id, tenant_id)

            # Get recipe base IDs and per-product multipliers (Issue #517)
            recipe_base_query = """
                SELECT product_base_type_id, quantity
                FROM product_base_recipes
                WHERE product_id = $1
                ORDER BY created_at
            """
            recipe_base_rows = await connection.fetch(recipe_base_query, product_id)

            # Get modifier groups with modifiers (using junction table)
            modifier_groups_query = """
                SELECT
                    mg.id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order
                FROM modifier_groups mg
                JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                WHERE pmg.product_id = $1
                ORDER BY mg.sort_order, mg.name
            """
            modifier_groups_rows = await connection.fetch(modifier_groups_query, product_id)

            # Get all modifiers in a single query
            if modifier_groups_rows:
                group_ids = [row['id'] for row in modifier_groups_rows]
                modifiers_query = """
                    SELECT
                        m.id,
                        m.modifier_group_id,
                        m.name,
                        m.price,
                        m.is_available,
                        m.is_default,
                        m.sort_order
                    FROM modifiers m
                    WHERE m.modifier_group_id = ANY($1::uuid[])
                    ORDER BY m.sort_order, m.name
                """
                modifiers_rows = await connection.fetch(modifiers_query, group_ids)

                # Group modifiers by modifier_group_id
                modifiers_by_group = {}
                for mod in modifiers_rows:
                    group_id = mod['modifier_group_id']
                    if group_id not in modifiers_by_group:
                        modifiers_by_group[group_id] = []
                    modifiers_by_group[group_id].append({
                        'id': mod['id'],
                        'name': mod['name'],
                        'price': mod['price'],
                        'is_available': mod['is_available'],
                        'is_default': mod['is_default'],
                        'sort_order': mod['sort_order']
                    })

                # Build modifier groups with their modifiers
                modifier_groups = []
                for mg_row in modifier_groups_rows:
                    group_dict = dict(mg_row)
                    group_dict['modifiers'] = modifiers_by_group.get(mg_row['id'], [])
                    modifier_groups.append(group_dict)
            else:
                modifier_groups = []

            # Build product dict
            product_dict = dict(product_row)
            # Build nested station object from joined kitchen_stations columns
            if product_dict.get('ks_id'):
                product_dict['station'] = {
                    'id': product_dict['ks_id'],
                    'name': product_dict['station_name'],
                    'color': product_dict['station_color'],
                }
            else:
                product_dict['station'] = None
            # Remove flat station columns (not part of Product model)
            product_dict.pop('ks_id', None)
            product_dict.pop('station_name', None)
            product_dict.pop('station_color', None)
            product_dict['ingredients'] = [dict(row) for row in recipe_rows]
            product_dict['recipe_base_ids'] = [row['product_base_type_id'] for row in recipe_base_rows]
            product_dict['recipe_bases'] = [
                {'recipe_base_id': row['product_base_type_id'], 'quantity': row['quantity']}
                for row in recipe_base_rows
            ]
            product_dict['modifier_groups'] = modifier_groups

            return ProductResponse(data=Product(**product_dict))

        if conn:
            return await _fetch_product(conn)
        else:
            async with get_db_connection() as connection:
                return await _fetch_product(connection)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching product: {str(e)}")
        raise APIError(f"Error fetching product: {str(e)}", status_code=500)


async def get_products_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    category_id: Optional[UUID] = None,
    is_available: Optional[bool] = None,
    is_combo: Optional[bool] = None,
    is_resale: Optional[bool] = None,
    station_id: Optional[UUID] = None,
    is_available_online: Optional[bool] = None,
    is_available_table_qr: Optional[bool] = None,
    has_recipe: Optional[bool] = None,
    margin_negative: Optional[bool] = None,
    sort: Optional[str] = None,
    include_ingredients: bool = False,
    include_modifiers: bool = False,
) -> ProductsListResponse:
    """Get list of products with filters"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # CTE: pre-compute latest purchase cost per ingredient once (O(1) index scan)
            # Replaces nested correlated subqueries that ran O(products × base_recipe_rows) times
            cte_prefix = cost_resolution_service.LIST_COST_CTE_PREFIX

            # Base query uses CTE JOINs instead of correlated subqueries
            base_query = """
                SELECT
                    p.id,
                    p.name,
                    p.description,
                    p.price,
                    p.category_id,
                    c.name as category_name,
                    p.preparation_time,
                    p.controla_stock,
                    p.is_available,
                    p.is_available_online,
                    p.is_available_table_qr,
                    p.is_combo,
                    p.is_resale,
                    p.open_priced,
                    p.allow_modifiers,
                    p.tax_category,
                    COALESCE(dc.direct_cost, 0) + COALESCE(bc.base_cost, 0) as costo_calculado,
                    p.costo_percibido,
                    p.precio_sugerido,
                    p.margen_objetivo,
                    p.tenant_id,
                    p.created_at,
                    p.updated_at,
                    p.station_id,
                    p.kitchen_name,
                    p.image_url,
                    ks.id as ks_id,
                    ks.name as station_name,
                    ks.color as station_color
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                LEFT JOIN direct_costs dc ON p.id = dc.product_id
                LEFT JOIN base_costs bc ON p.id = bc.product_id
                LEFT JOIN tenant_category_stations tcs ON tcs.category_id = p.category_id AND tcs.tenant_id = p.tenant_id
                LEFT JOIN kitchen_stations ks ON ks.id = COALESCE(p.station_id, tcs.station_id)
                WHERE p.tenant_id = $1
            """

            # Add calculated margin fields after the main query
            base_query = cte_prefix + """
                SELECT
                    *,
            """ + _PRODUCT_MARGIN_OUTER_SQL + """
                FROM (
            """ + base_query + """
                ) subquery
                WHERE 1=1
            """

            params = [tenant_id]
            param_count = 2
            inner_filters = ""

            # Inner filters (product / join predicates)
            if station_id is not None:
                inner_filters += (
                    f" AND (p.station_id = ${param_count}"
                    f" OR tcs.station_id = ${param_count})"
                )
                params.append(station_id)
                param_count += 1

            if is_available_online is not None:
                inner_filters += f" AND p.is_available_online = ${param_count}"
                params.append(is_available_online)
                param_count += 1

            if is_available_table_qr is not None:
                inner_filters += f" AND p.is_available_table_qr = ${param_count}"
                params.append(is_available_table_qr)
                param_count += 1

            if has_recipe is not None:
                if has_recipe:
                    inner_filters += f" AND {_HAS_RECIPE_SQL}"
                else:
                    inner_filters += f" AND NOT {_HAS_RECIPE_SQL}"

            if inner_filters:
                base_query = base_query.replace(
                    "WHERE p.tenant_id = $1",
                    "WHERE p.tenant_id = $1" + inner_filters,
                    1,
                )

            # Outer filters (subquery column aliases)
            if search:
                term = f"%{search}%"
                if search_field:
                    field = search_field.strip().lower()
                    if field not in _PRODUCT_SEARCH_FIELDS:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Invalid search_field '{search_field}'. "
                                f"Allowed: {', '.join(sorted(_PRODUCT_SEARCH_FIELDS))}"
                            ),
                        )
                    base_query += f" AND LOWER({field}) LIKE LOWER(${param_count})"
                    params.append(term)
                    param_count += 1
                else:
                    base_query += (
                        f" AND (LOWER(name) LIKE LOWER(${param_count})"
                        f" OR LOWER(description) LIKE LOWER(${param_count}))"
                    )
                    params.append(term)
                    param_count += 1

            if category_id:
                base_query += f" AND category_id = ${param_count}"
                params.append(category_id)
                param_count += 1

            if is_available is not None:
                base_query += f" AND is_available = ${param_count}"
                params.append(is_available)
                param_count += 1

            if is_combo is not None:
                base_query += f" AND is_combo = ${param_count}"
                params.append(is_combo)
                param_count += 1

            # Filter by is_resale - default to excluding resale products
            # Resale products have their own section, so by default we exclude them
            # Exception: POS (include_modifiers=true) should show ALL products
            if is_resale is None:
                if not include_modifiers:
                    base_query += " AND (is_resale = false OR is_resale IS NULL)"
            else:
                base_query += f" AND is_resale = ${param_count}"
                params.append(is_resale)
                param_count += 1

            if margin_negative is not None:
                if margin_negative:
                    base_query += (
                        " AND costo_calculado IS NOT NULL"
                        " AND costo_calculado > price"
                    )
                else:
                    base_query += (
                        " AND (costo_calculado IS NULL"
                        " OR costo_calculado <= price)"
                    )

            order_clause = _resolve_products_sort(sort)
            offset = (page - 1) * limit
            count_query = f"SELECT COUNT(*) FROM ({base_query}) AS counted"
            base_query += (
                f" ORDER BY {order_clause} LIMIT ${param_count} OFFSET ${param_count + 1}"
            )

            # Execute queries
            products_data = await conn.fetch(base_query, *params, limit, offset)
            count_result = await conn.fetchrow(count_query, *params)

            total = count_result['count']

            # Convert to Product models
            products = []
            for row in products_data:
                product_dict = dict(row)

                # Fetch ingredients if requested
                if include_ingredients:
                    recipe_query = """
                        SELECT
                            pr.id,
                            pr.product_id,
                            pr.ingredient_id,
                            pr.quantity,
                            pr.unit,
                            i.name as ingredient_name,
                            COALESCE(
                                (SELECT pi.unit_cost
                                 FROM tenant_purchase_items pi
                                 JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                                 WHERE pi.ingredient_id = pr.ingredient_id
                                   AND tp.tenant_id = $2
                                   AND pi.unit_cost IS NOT NULL AND pi.unit_cost > 0
                                 ORDER BY tp.purchase_date DESC LIMIT 1),
                                i.costo_unitario, 0
                            ) as ingredient_cost_per_unit,
                            pr.quantity * COALESCE(
                                (SELECT pi.unit_cost
                                 FROM tenant_purchase_items pi
                                 JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                                 WHERE pi.ingredient_id = pr.ingredient_id
                                   AND tp.tenant_id = $2
                                   AND pi.unit_cost IS NOT NULL AND pi.unit_cost > 0
                                 ORDER BY tp.purchase_date DESC LIMIT 1),
                                i.costo_unitario, 0
                            ) as total_cost
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = $1
                        ORDER BY i.name
                    """
                    recipe_rows = await conn.fetch(recipe_query, row['id'], tenant_id)
                    product_dict['ingredients'] = [dict(r) for r in recipe_rows]
                else:
                    product_dict['ingredients'] = []  # Empty for list view

                # Fetch modifier groups if requested (for POS) - using junction table
                if include_modifiers:
                    modifier_groups_query = """
                        SELECT
                            mg.id,
                            mg.name,
                            mg.min_qty,
                            mg.max_qty,
                            mg.is_required,
                            mg.sort_order
                        FROM modifier_groups mg
                        JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                        WHERE pmg.product_id = $1
                        ORDER BY mg.sort_order, mg.name
                    """
                    modifier_groups_rows = await conn.fetch(modifier_groups_query, row['id'])

                    if modifier_groups_rows:
                        group_ids = [mg_row['id'] for mg_row in modifier_groups_rows]
                        modifiers_query = """
                            SELECT
                                m.id,
                                m.modifier_group_id,
                                m.name,
                                m.price,
                                m.is_available,
                                m.is_default,
                                m.sort_order
                            FROM modifiers m
                            WHERE m.modifier_group_id = ANY($1::uuid[])
                            ORDER BY m.sort_order, m.name
                        """
                        modifiers_rows = await conn.fetch(modifiers_query, group_ids)

                        # Group modifiers by modifier_group_id
                        modifiers_by_group = {}
                        for mod in modifiers_rows:
                            group_id = mod['modifier_group_id']
                            if group_id not in modifiers_by_group:
                                modifiers_by_group[group_id] = []
                            modifiers_by_group[group_id].append({
                                'id': mod['id'],
                                'name': mod['name'],
                                'price': mod['price'],
                                'is_available': mod['is_available'],
                                'is_default': mod['is_default'],
                                'sort_order': mod['sort_order']
                            })

                        # Build modifier groups with their modifiers
                        modifier_groups = []
                        for mg_row in modifier_groups_rows:
                            group_dict = dict(mg_row)
                            group_dict['modifiers'] = modifiers_by_group.get(mg_row['id'], [])
                            modifier_groups.append(group_dict)

                        product_dict['modifier_groups'] = modifier_groups
                    else:
                        product_dict['modifier_groups'] = []
                else:
                    product_dict['modifier_groups'] = []  # Empty for list view

                # Build nested station object from joined kitchen_stations columns
                if product_dict.get('ks_id'):
                    product_dict['station'] = {
                        'id': product_dict['ks_id'],
                        'name': product_dict['station_name'],
                        'color': product_dict['station_color'],
                    }
                else:
                    product_dict['station'] = None
                product_dict.pop('ks_id', None)
                product_dict.pop('station_name', None)
                product_dict.pop('station_color', None)

                products.append(Product(**product_dict))

            return ProductsListResponse(
                success=True,
                total=total,
                data=products
            )

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching products: {str(e)}")
        raise APIError(f"Error fetching products: {str(e)}", status_code=500)


async def get_product_stats(request: Request) -> ProductStats:
    """Get product statistics for dashboard"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            stats_query = """
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE is_available = TRUE) as available,
                    COUNT(*) FILTER (WHERE controla_stock = TRUE) as with_stock_control,
                    COUNT(*) FILTER (WHERE is_combo = TRUE) as combos,
                    AVG(
                        CASE
                            WHEN costo_calculado > 0
                            THEN ((price - costo_calculado) / costo_calculado * 100)
                            ELSE NULL
                        END
                    ) as avg_margin
                FROM product
                WHERE tenant_id = $1
            """

            stats_row = await conn.fetchrow(stats_query, tenant_id)

            return ProductStats(**dict(stats_row))

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching product stats: {str(e)}")
        raise APIError(f"Error fetching product stats: {str(e)}", status_code=500)


async def update_product_with_recipe(
    request: Request,
    product_id: UUID,
    product_data: ProductUpdate
) -> ProductResponse:
    """
    Updates a product with its recipe in a single transaction.
    Steps:
    1. Update product fields
    2. If ingredients provided, replace recipe (delete old, insert new)
    3. Recalculate and update product cost
    4. Return complete product with recipe
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify product exists and belongs to tenant
            verify_query = "SELECT id, name FROM product WHERE id = $1 AND tenant_id = $2"
            product_exists = await conn.fetchrow(verify_query, product_id, tenant_id)

            if not product_exists:
                raise HTTPException(status_code=404, detail="Product not found")

            # Obtener snapshot ANTES de actualizar (para historial)
            old_snapshot = await menu_history_service.get_product_snapshot(conn, product_id, tenant_id)
            product_name = product_exists['name']
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

            # Start transaction
            async with conn.transaction():
                if product_data.open_priced is True:
                    await assert_single_open_priced_per_tenant(
                        conn,
                        tenant_id,
                        exclude_product_id=product_id,
                    )

                # 1. Build update query dynamically based on provided fields
                # NOTE: controla_stock is ALWAYS excluded from updates - it's always True
                update_fields = []
                update_values = []
                param_count = 1

                # Fields where None is a valid "clear this value" intent (#465).
                # Without this, the loop below silently drops attempts to remove
                # an image when the user clicks "Eliminar imagen" in the form.
                NULLABLE_FIELDS = {'image_url', 'costo_percibido'}
                for field, value in product_data.dict(exclude={'ingredients', 'recipe_base_ids', 'recipe_bases', 'controla_stock'}, exclude_unset=True).items():
                    if value is None and field not in NULLABLE_FIELDS:
                        continue
                    update_fields.append(f"{field} = ${param_count}")
                    update_values.append(value)
                    param_count += 1

                # Force controla_stock to always be True
                update_fields.append("controla_stock = TRUE")

                if update_fields:
                    update_fields.append("updated_at = NOW()")
                    update_query = f"""
                        UPDATE product
                        SET {', '.join(update_fields)}
                        WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                    """
                    update_values.extend([product_id, tenant_id])
                    await conn.execute(update_query, *update_values)

                # 2. Update recipe base associations if provided (Issue #517 — with quantity).
                # Either of the two fields (recipe_bases / recipe_base_ids) being set means
                # the caller intends to replace the full set of links.
                bases_provided = (
                    product_data.recipe_bases is not None
                    or product_data.recipe_base_ids is not None
                )
                if bases_provided:
                    normalized_bases = _normalize_recipe_bases(
                        product_data.recipe_bases,
                        product_data.recipe_base_ids,
                    )

                    # Delete existing associations
                    delete_base_recipe_query = "DELETE FROM product_base_recipes WHERE product_id = $1"
                    await conn.execute(delete_base_recipe_query, product_id)

                    # Insert new associations with per-product quantity
                    if normalized_bases:
                        base_recipe_query = """
                            INSERT INTO product_base_recipes (
                                product_id, product_base_type_id, tenant_id, quantity
                            )
                            VALUES ($1, $2, $3, $4)
                        """
                        for recipe_base_id, base_qty in normalized_bases:
                            await conn.execute(
                                base_recipe_query,
                                product_id,
                                recipe_base_id,
                                tenant_id,
                                base_qty,
                            )

                # 3. Update recipe if ingredients provided.
                # Empty list is now valid — product becomes "no inventory tracking".
                if product_data.ingredients is not None:
                    # Delete existing recipe
                    delete_recipe_query = "DELETE FROM product_recipes WHERE product_id = $1"
                    await conn.execute(delete_recipe_query, product_id)

                    # Insert new recipe
                    if product_data.ingredients:
                        recipe_query = """
                            INSERT INTO product_recipes (
                                product_id, ingredient_id, quantity, unit, tenant_id
                            )
                            VALUES ($1, $2, $3, $4, $5)
                        """

                        for ingredient in product_data.ingredients:
                            base_qty, base_unit = await resolve_to_base_unit(
                                conn,
                                ingredient.ingredient_id,
                                ingredient.quantity,
                                ingredient.unit
                            )
                            await conn.execute(
                                recipe_query,
                                product_id,
                                ingredient.ingredient_id,
                                base_qty,
                                base_unit,
                                tenant_id
                            )

                # 4. Recalculate real cost only (never touches costo_percibido).
                has_any_recipe = await cost_resolution_service.product_has_any_recipe(
                    product_id, conn
                )
                await cost_resolution_service.persist_product_costo_calculado(
                    product_id,
                    tenant_id,
                    conn,
                    tracks_inventory=has_any_recipe,
                )

                # 5. Registrar cambios en historial
                if old_snapshot:
                    new_snapshot = await menu_history_service.get_product_snapshot(conn, product_id, tenant_id)
                    if new_snapshot:
                        await menu_history_service.compare_and_record_product_changes(
                            conn, tenant_id, product_id, product_name,
                            old_snapshot, new_snapshot, user_id
                        )

                # 6. Get complete updated product
                return await get_product_by_id(request, product_id, conn)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=_DUPLICATE_PRODUCT_NAME_DETAIL)
    except Exception as e:
        logger.error(f"Error updating product: {str(e)}")
        raise APIError(f"Error updating product: {str(e)}", status_code=500)


async def delete_product(
    request: Request,
    product_id: UUID
) -> dict:
    """
    Deletes a product when it has no sales history, or archives it when order_items exist.

    Hard delete would CASCADE into order_items and fail on comanda_items FK (warocol.com#705).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            verify_query = """
                SELECT id, name, is_available, is_available_online
                FROM product WHERE id = $1 AND tenant_id = $2
            """
            product_row = await conn.fetchrow(verify_query, product_id, tenant_id)

            if not product_row:
                raise HTTPException(status_code=404, detail="Product not found")

            product_snapshot = await menu_history_service.get_product_snapshot(conn, product_id, tenant_id)
            product_name = product_row['name']
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

            async with conn.transaction():
                has_sales = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM order_items WHERE product_id = $1)",
                    product_id,
                )

                if has_sales:
                    if not product_row['is_available'] and not product_row['is_available_online']:
                        return {
                            "success": True,
                            "archived": True,
                            "message": "Product is already archived",
                        }

                    await conn.execute(
                        """
                        UPDATE product
                           SET is_available = false,
                               is_available_online = false,
                               updated_at = NOW()
                         WHERE id = $1 AND tenant_id = $2
                        """,
                        product_id,
                        tenant_id,
                    )

                    archive_reason = "Archived: product has sales history (order_items)"
                    if product_row['is_available']:
                        await menu_history_service.record_product_update(
                            conn, tenant_id, product_id, product_name,
                            'is_available', product_row['is_available'], False,
                            user_id, archive_reason,
                        )
                    if product_row['is_available_online']:
                        await menu_history_service.record_product_update(
                            conn, tenant_id, product_id, product_name,
                            'is_available_online', product_row['is_available_online'], False,
                            user_id, archive_reason,
                        )

                    return {
                        "success": True,
                        "archived": True,
                        "message": (
                            "Product archived. It remains in sales history and is hidden "
                            "from POS and online ordering."
                        ),
                    }

                if product_snapshot:
                    await menu_history_service.record_product_delete(
                        conn, tenant_id, product_id, product_name,
                        product_snapshot, user_id
                    )

                await conn.execute(
                    "DELETE FROM product_recipes WHERE product_id = $1",
                    product_id,
                )
                await conn.execute(
                    "DELETE FROM product WHERE id = $1 AND tenant_id = $2",
                    product_id, tenant_id,
                )

                return {
                    "success": True,
                    "archived": False,
                    "message": "Product deleted successfully",
                }

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting product: {str(e)}")
        raise APIError(f"Error deleting product: {str(e)}", status_code=500)


async def upload_product_image(
    request: Request,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> Optional[str]:
    """Upload a product hero image to Cloudflare R2 (public bucket).

    Mirrors `tenant_config_service.upload_tenant_image` but writes to the
    `product-images/{tenant_id}/{uuid}.{ext}` prefix. Validation (MIME +
    size) is enforced at the router layer; this service only auths + uploads.

    Returns the permanent public URL or raises HTTPException 500 on failure.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context is required")

    s3_service = AWSS3Service()
    public_url = await s3_service.upload_public_image(
        file_bytes=file_bytes,
        filename=filename,
        tenant_id=str(tenant_id),
        image_type='product',
        content_type=content_type,
    )

    if not public_url:
        logger.error(f"upload_product_image returned None for tenant {tenant_id}")
        raise HTTPException(
            status_code=500,
            detail="No se pudo subir la imagen. Intenta de nuevo.",
        )

    return public_url
