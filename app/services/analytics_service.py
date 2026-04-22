"""
Analytics Service
Provides menu analysis, food cost tracking, and system alerts
"""
from typing import Optional, List, Dict, Any
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from datetime import datetime, date, timedelta
from statistics import mean as st_mean, quantiles
import logging

logger = logging.getLogger(__name__)


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Convert date string (YYYY-MM-DD) to date object"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


async def _get_menu_analysis_for_tenant(
    tenant_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 10
) -> dict:
    """Auth-agnostic core for menu analysis. Called by session wrapper and public API."""
    # Default to start of current year
    parsed_date_from = parse_date(date_from)
    parsed_date_to = parse_date(date_to)

    if not parsed_date_from or not parsed_date_to:
        today = datetime.now().date()
        parsed_date_to = today
        parsed_date_from = today.replace(month=1, day=1)

    async with get_db_connection() as conn:
        # Get product sales with REAL profitability based on purchase history
        # Calculates actual cost from recipe ingredients and their latest purchase costs
        # Optimized query: Pre-calculate latest ingredient costs first, then join.
        # This avoids N*M subqueries and uses a single efficient CTE for cost lookup.
        query = """
            WITH latest_ingredient_costs AS (
                -- Get the most recent purchase cost for each ingredient
                SELECT DISTINCT ON (pi.ingredient_id)
                    pi.ingredient_id,
                    pi.unit as purchase_unit,
                    pi.unit_cost,
                    ipu.conversion_factor,
                    ipu.purchase_unit_label
                FROM tenant_purchase_items pi
                JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                LEFT JOIN ingredient_purchase_units ipu ON
                    pi.ingredient_id = ipu.ingredient_id AND
                    pi.unit = ipu.purchase_unit
                WHERE tp.tenant_id = $1
                  AND pi.unit_cost IS NOT NULL
                  AND pi.unit_cost > 0
                ORDER BY pi.ingredient_id, tp.purchase_date DESC
            ),
            product_ingredients_costs AS (
                SELECT
                    p.id as product_id,
                    SUM(
                        CASE
                            -- Direct match (units are same or aliases)
                            WHEN (pr.unit = lic.purchase_unit)
                              OR (pr.unit IN ('g', 'gr') AND lic.purchase_unit IN ('g', 'gr'))
                              OR (pr.unit IN ('u', 'und') AND lic.purchase_unit IN ('u', 'und'))
                            THEN pr.quantity * lic.unit_cost
                            -- Conversion needed
                            WHEN lic.conversion_factor > 0 THEN
                                pr.quantity * (lic.unit_cost / lic.conversion_factor)
                            -- Fallback to current configured cost if no purchase history or funny units
                            ELSE pr.quantity * i.costo_unitario
                        END
                    ) as total_recipe_cost
                FROM product p
                JOIN product_recipes pr ON p.id = pr.product_id
                JOIN ingredients i ON pr.ingredient_id = i.id
                LEFT JOIN latest_ingredient_costs lic ON pr.ingredient_id = lic.ingredient_id
                WHERE p.tenant_id = $1
                GROUP BY p.id
            ),
            base_recipe_costs AS (
                 SELECT
                    p.id as product_id,
                    SUM(
                        CASE
                            -- Direct match
                            WHEN (brt.unit = lic.purchase_unit)
                              OR (brt.unit IN ('g', 'gr') AND lic.purchase_unit IN ('g', 'gr'))
                              OR (brt.unit IN ('u', 'und') AND lic.purchase_unit IN ('u', 'und'))
                            THEN brt.base_quantity * lic.unit_cost
                            -- Conversion
                            WHEN lic.conversion_factor > 0 THEN
                                brt.base_quantity * (lic.unit_cost / lic.conversion_factor)
                            -- Fallback
                            ELSE brt.base_quantity * i.costo_unitario
                        END
                    ) as total_base_cost
                FROM product p
                JOIN product_base_recipes pbr ON p.id = pbr.product_id
                JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                JOIN ingredients i ON brt.ingredient_id = i.id
                LEFT JOIN latest_ingredient_costs lic ON brt.ingredient_id = lic.ingredient_id
                WHERE p.tenant_id = $1
                GROUP BY p.id
            ),
            product_real_costs AS (
                SELECT
                    p.id,
                    p.price,
                    COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0) as calc_cost,
                    CASE
                        WHEN (COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0)) = 0 THEN p.price * 0.40 -- Fallback if no cost info
                        WHEN (COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0)) > p.price THEN p.price * 0.40 -- Fallback if error
                        ELSE (COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0))
                    END as real_cost
                FROM product p
                LEFT JOIN product_ingredients_costs pic ON p.id = pic.product_id
                LEFT JOIN base_recipe_costs brc ON p.id = brc.product_id
                WHERE p.tenant_id = $1
            ),
            product_sales AS (
                SELECT
                    p.id,
                    p.name,
                    p.price,
                    prc.real_cost as estimated_cost,
                    c.name as category_name,
                    COUNT(DISTINCT oi.id) as order_count,
                    SUM(oi.quantity) as total_units_sold,
                    SUM(COALESCE(oi.net_total, oi.subtotal)) as total_revenue,
                    AVG(oi.price_at_purchase) as avg_price
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                LEFT JOIN product_real_costs prc ON p.id = prc.id
                LEFT JOIN order_items oi ON p.id = oi.product_id
                LEFT JOIN orders o ON oi.order_id = o.id
                WHERE p.tenant_id = $1
                    AND p.is_available = true
                    AND (
                        o.id IS NULL OR (
                            o.status = 'completed'
                            AND DATE(o.order_date AT TIME ZONE 'America/Bogota') >= $2
                            AND DATE(o.order_date AT TIME ZONE 'America/Bogota') <= $3
                        )
                    )
                GROUP BY p.id, p.name, p.price, prc.real_cost, c.name
                HAVING COUNT(DISTINCT oi.id) > 0
            ),
            sales_stats AS (
                SELECT
                    AVG(total_units_sold) as avg_units,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_units_sold) as median_units,
                    AVG((price - estimated_cost) / NULLIF(price, 0) * 100) as avg_margin_pct,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (price - estimated_cost) / NULLIF(price, 0) * 100) as median_margin_pct
                FROM product_sales
            )
            SELECT
                ps.*,
                (ps.price - ps.estimated_cost) as profit_per_unit,
                ((ps.price - ps.estimated_cost) / NULLIF(ps.price, 0) * 100) as profit_margin_pct,
                (ps.price - ps.estimated_cost) * ps.total_units_sold as total_profit,
                CASE
                    WHEN ps.total_units_sold >= ss.median_units
                        AND ((ps.price - ps.estimated_cost) / NULLIF(ps.price, 0) * 100) >= ss.median_margin_pct
                        THEN 'Star'
                    WHEN ps.total_units_sold >= ss.median_units
                        AND ((ps.price - ps.estimated_cost) / NULLIF(ps.price, 0) * 100) < ss.median_margin_pct
                        THEN 'Plowhorse'
                    WHEN ps.total_units_sold < ss.median_units
                        AND ((ps.price - ps.estimated_cost) / NULLIF(ps.price, 0) * 100) >= ss.median_margin_pct
                        THEN 'Puzzle'
                    ELSE 'Dog'
                END as classification
            FROM product_sales ps
            CROSS JOIN sales_stats ss
            ORDER BY ps.total_revenue DESC
            LIMIT $4
        """

        rows = await conn.fetch(
            query,
            tenant_id,
            parsed_date_from,
            parsed_date_to,
            limit
        )

        menu_items = []
        for row in rows:
            menu_items.append({
                "id": str(row['id']),
                "name": row['name'],
                "category": row['category_name'],
                "price": float(row['price']),
                "estimated_cost": float(row['estimated_cost']),
                "profit_per_unit": float(row['profit_per_unit']),
                "profit_margin_pct": round(float(row['profit_margin_pct']), 1),
                "order_count": row['order_count'],
                "total_units_sold": row['total_units_sold'],
                "total_revenue": float(row['total_revenue']),
                "total_profit": float(row['total_profit']),
                "avg_price": float(row['avg_price']),
                "classification": row['classification']
            })

        # Calculate summary stats
        if menu_items:
            total_items = len(menu_items)
            stars = sum(1 for item in menu_items if item['classification'] == 'Star')
            plowhorses = sum(1 for item in menu_items if item['classification'] == 'Plowhorse')
            puzzles = sum(1 for item in menu_items if item['classification'] == 'Puzzle')
            dogs = sum(1 for item in menu_items if item['classification'] == 'Dog')

            avg_margin = sum(item['profit_margin_pct'] for item in menu_items) / total_items
        else:
            total_items = stars = plowhorses = puzzles = dogs = 0
            avg_margin = 0.0

        return {
            "success": True,
            "data": {
                "menu_items": menu_items,
                "summary": {
                    "total_items": total_items,
                    "stars": stars,
                    "plowhorses": plowhorses,
                    "puzzles": puzzles,
                    "dogs": dogs,
                    "avg_profit_margin_pct": round(avg_margin, 1)
                },
                "period": {
                    "from": parsed_date_from.isoformat(),
                    "to": parsed_date_to.isoformat(),
                    "days": (parsed_date_to - parsed_date_from).days + 1
                }
            }
        }


async def get_menu_analysis(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 10
) -> dict:
    """
    Get menu analysis with product classification based on profitability and popularity

    Classification (BCG Matrix):
    - Stars: High profit margin, high sales volume (keep and promote)
    - Plowhorses: Low profit margin, high sales volume (optimize costs)
    - Puzzles: High profit margin, low sales volume (increase marketing)
    - Dogs: Low profit margin, low sales volume (consider removing)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")
        return await _get_menu_analysis_for_tenant(tenant_id, date_from, date_to, limit)
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting menu analysis: {str(e)}")
        raise APIError(f"Error getting menu analysis: {str(e)}", status_code=500)


async def _get_food_cost_for_tenant(
    tenant_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    compare_to: Optional[str] = None,
    compare_from: Optional[str] = None,
    compare_date_to: Optional[str] = None,
) -> dict:
    """Auth-agnostic core for food cost. Called by session wrapper and public API."""
    # Default to start of current year
    parsed_date_from = parse_date(date_from)
    parsed_date_to = parse_date(date_to)

    if not parsed_date_from or not parsed_date_to:
        today = datetime.now().date()
        parsed_date_to = today
        parsed_date_from = today.replace(month=1, day=1)

    # Determine comparison window based on compare_to mode.
    # Default (None or "previous_period") mirrors the same duration immediately before date_from.
    if compare_to == "previous_year":
        prev_date_from = parsed_date_from - timedelta(days=365)
        prev_date_to = parsed_date_to - timedelta(days=365)
    elif compare_to == "custom":
        cf = parse_date(compare_from)
        ct = parse_date(compare_date_to)
        prev_date_from = cf if cf else parsed_date_from - timedelta(days=(parsed_date_to - parsed_date_from).days + 1)
        prev_date_to = ct if ct else parsed_date_from - timedelta(days=1)
    else:
        # "previous_period" (default) — same duration immediately before
        days_diff = (parsed_date_to - parsed_date_from).days + 1
        prev_date_to = parsed_date_from - timedelta(days=1)
        prev_date_from = prev_date_to - timedelta(days=days_diff - 1)

    async with get_db_connection() as conn:
        # Query for current and previous period with REAL costs from purchase history
        # Optimized query: Pre-calculate latest ingredient costs first, then join.
        # This avoids N*M subqueries and uses a single efficient CTE for cost lookup.
        query = """
            WITH latest_ingredient_costs AS (
                -- Get the most recent purchase cost for each ingredient
                SELECT DISTINCT ON (pi.ingredient_id)
                    pi.ingredient_id,
                    pi.unit as purchase_unit,
                    pi.unit_cost,
                    ipu.conversion_factor,
                    ipu.purchase_unit_label
                FROM tenant_purchase_items pi
                JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                LEFT JOIN ingredient_purchase_units ipu ON
                    pi.ingredient_id = ipu.ingredient_id AND
                    pi.unit = ipu.purchase_unit
                WHERE tp.tenant_id = $1
                  AND pi.unit_cost IS NOT NULL
                  AND pi.unit_cost > 0
                ORDER BY pi.ingredient_id, tp.purchase_date DESC
            ),
            product_ingredients_costs AS (
                SELECT
                    p.id as product_id,
                    SUM(
                        CASE
                            -- Direct match (units are same or aliases)
                            WHEN (pr.unit = lic.purchase_unit)
                              OR (pr.unit IN ('g', 'gr') AND lic.purchase_unit IN ('g', 'gr'))
                              OR (pr.unit IN ('u', 'und') AND lic.purchase_unit IN ('u', 'und'))
                            THEN pr.quantity * lic.unit_cost
                            -- Conversion needed
                            WHEN lic.conversion_factor > 0 THEN
                                pr.quantity * (lic.unit_cost / lic.conversion_factor)
                            -- Fallback to current configured cost if no purchase history or funny units
                            ELSE pr.quantity * i.costo_unitario
                        END
                    ) as total_recipe_cost
                FROM product p
                JOIN product_recipes pr ON p.id = pr.product_id
                JOIN ingredients i ON pr.ingredient_id = i.id
                LEFT JOIN latest_ingredient_costs lic ON pr.ingredient_id = lic.ingredient_id
                WHERE p.tenant_id = $1
                GROUP BY p.id
            ),
            base_recipe_costs AS (
                 SELECT
                    p.id as product_id,
                    SUM(
                        CASE
                            -- Direct match
                            WHEN (brt.unit = lic.purchase_unit)
                              OR (brt.unit IN ('g', 'gr') AND lic.purchase_unit IN ('g', 'gr'))
                              OR (brt.unit IN ('u', 'und') AND lic.purchase_unit IN ('u', 'und'))
                            THEN brt.base_quantity * lic.unit_cost
                            -- Conversion
                            WHEN lic.conversion_factor > 0 THEN
                                brt.base_quantity * (lic.unit_cost / lic.conversion_factor)
                            -- Fallback
                            ELSE brt.base_quantity * i.costo_unitario
                        END
                    ) as total_base_cost
                FROM product p
                JOIN product_base_recipes pbr ON p.id = pbr.product_id
                JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                JOIN ingredients i ON brt.ingredient_id = i.id
                LEFT JOIN latest_ingredient_costs lic ON brt.ingredient_id = lic.ingredient_id
                WHERE p.tenant_id = $1
                GROUP BY p.id
            ),
            product_real_costs AS (
                SELECT
                    p.id,
                    p.price,
                    COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0) as calc_cost,
                    CASE
                        WHEN (COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0)) = 0 THEN p.price * 0.40 -- Fallback if no cost info
                        WHEN (COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0)) > p.price THEN p.price * 0.40 -- Fallback if error
                        ELSE (COALESCE(pic.total_recipe_cost, 0) + COALESCE(brc.total_base_cost, 0))
                    END as real_cost
                FROM product p
                LEFT JOIN product_ingredients_costs pic ON p.id = pic.product_id
                LEFT JOIN base_recipe_costs brc ON p.id = brc.product_id
                WHERE p.tenant_id = $1
            ),
            period_costs AS (
                SELECT
                    SUM(COALESCE(oi.net_total, oi.subtotal)) as revenue,
                    SUM(oi.quantity * prc.real_cost) as total_cost,
                    CASE
                        WHEN DATE(o.order_date AT TIME ZONE 'America/Bogota') >= $2
                            AND DATE(o.order_date AT TIME ZONE 'America/Bogota') <= $3
                        THEN 'current'
                        ELSE 'previous'
                    END as period
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.id
                JOIN product p ON oi.product_id = p.id
                JOIN product_real_costs prc ON p.id = prc.id
                WHERE o.tenant_id = $1
                    AND o.status = 'completed'
                    AND (
                        (DATE(o.order_date AT TIME ZONE 'America/Bogota') >= $2
                            AND DATE(o.order_date AT TIME ZONE 'America/Bogota') <= $3)
                        OR
                        (DATE(o.order_date AT TIME ZONE 'America/Bogota') >= $4
                            AND DATE(o.order_date AT TIME ZONE 'America/Bogota') <= $5)
                    )
                GROUP BY period
            )
            SELECT
                period,
                revenue,
                total_cost,
                (total_cost / NULLIF(revenue, 0) * 100) as food_cost_pct
            FROM period_costs
        """

        rows = await conn.fetch(
            query,
            tenant_id,
            parsed_date_from,
            parsed_date_to,
            prev_date_from,
            prev_date_to
        )

        # Parse results
        current_data = {"revenue": 0, "total_cost": 0, "food_cost_pct": 0}
        previous_data = {"revenue": 0, "total_cost": 0, "food_cost_pct": 0}

        for row in rows:
            data = {
                "revenue": float(row['revenue'] or 0),
                "total_cost": float(row['total_cost'] or 0),
                "food_cost_pct": float(row['food_cost_pct'] or 0)
            }
            if row['period'] == 'current':
                current_data = data
            else:
                previous_data = data

        # Calculate change
        if previous_data['food_cost_pct'] > 0:
            change_pct = current_data['food_cost_pct'] - previous_data['food_cost_pct']
            change_type = 'increase' if change_pct > 0 else 'decrease' if change_pct < 0 else 'neutral'
        else:
            change_pct = 0
            change_type = 'neutral'

        return {
            "success": True,
            "data": {
                "current_period": {
                    "food_cost_pct": round(current_data['food_cost_pct'], 2),
                    "revenue": round(current_data['revenue'], 2),
                    "total_cost": round(current_data['total_cost'], 2),
                    "from": parsed_date_from.isoformat(),
                    "to": parsed_date_to.isoformat()
                },
                "previous_period": {
                    "food_cost_pct": round(previous_data['food_cost_pct'], 2),
                    "revenue": round(previous_data['revenue'], 2),
                    "total_cost": round(previous_data['total_cost'], 2),
                    "from": prev_date_from.isoformat(),
                    "to": prev_date_to.isoformat()
                },
                "comparison": {
                    "change_pct": round(change_pct, 2),
                    "change_type": change_type
                },
                "benchmark": {
                    "min_healthy": 28.0,
                    "max_healthy": 35.0,
                    "status": "good" if 28 <= current_data['food_cost_pct'] <= 35 else "warning"
                }
            }
        }



async def get_food_cost(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> dict:
    """
    Get food cost percentage with comparison to previous period

    Food Cost % = (Total Cost of Goods Sold / Total Revenue) * 100
    Industry standard: 28-35%
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")
        return await _get_food_cost_for_tenant(tenant_id, date_from, date_to)
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting food cost: {str(e)}")
        raise APIError(f"Error getting food cost: {str(e)}", status_code=500)


async def _get_alerts_for_tenant(
    tenant_id: str,
    limit: int = 10
) -> dict:
    """Auth-agnostic core for alerts. Called by session wrapper and public API."""
    alerts = []

    async with get_db_connection() as conn:
        # 1. CRITICAL: Ingredients with zero stock but active consumption (POS-only detection)
        zero_stock_active_query = """
            WITH recent_consumption AS (
                SELECT
                    tim.ingredient_id,
                    SUM(ABS(tim.quantity_change)) as consumed_7d,
                    (SUM(ABS(tim.quantity_change)) / 7.0) as daily_rate
                FROM tenant_ingredient_movements tim
                WHERE tim.tenant_id = $1
                    AND tim.movement_type = 'consumption'
                    AND tim.created_at >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY tim.ingredient_id
            )
            SELECT
                i.name,
                i.unit,
                ti.current_stock,
                rc.consumed_7d,
                rc.daily_rate,
                CEIL(rc.daily_rate * 14) as recommended_14d_order,
                (
                    SELECT COUNT(DISTINCT pr.product_id)
                    FROM product_recipes pr
                    WHERE pr.ingredient_id = i.id
                ) as products_affected
            FROM tenant_inventory ti
            JOIN ingredients i ON ti.ingredient_id = i.id
            JOIN recent_consumption rc ON rc.ingredient_id = i.id
            WHERE ti.tenant_id = $1
                AND ti.current_stock <= 0
                AND rc.consumed_7d > 0
            ORDER BY rc.daily_rate DESC
            LIMIT 5
        """
        zero_stock_rows = await conn.fetch(zero_stock_active_query, tenant_id)

        for row in zero_stock_rows:
            daily_rate = float(row['daily_rate'])
            recommended = float(row['recommended_14d_order'])
            products_affected = row['products_affected']

            title = f"⚠️ Sin stock: {row['name']}"
            description = f"Consumo activo: {daily_rate:.0f}{row['unit']}/día. Orden sugerida: {recommended:.0f}{row['unit']} (14 días). Afecta {products_affected} productos."

            alerts.append({
                "id": f"zero_stock_{len(alerts)}",
                "type": "critical",
                "title": title,
                "description": description,
                "action": {
                    "label": "Pedir ahora",
                    "url": "/abastecimiento/compras/crear"
                }
            })

        # 2. Top selling products blocked by missing ingredients
        if len(alerts) < limit:
            blocked_products_query = """
                WITH top_products AS (
                    SELECT
                        p.id,
                        p.name,
                        COUNT(DISTINCT oi.id) as order_count,
                        SUM(oi.quantity) as total_units
                    FROM order_items oi
                    JOIN orders o ON oi.order_id = o.id
                    JOIN product p ON oi.product_id = p.id
                    WHERE o.tenant_id = $1
                        AND o.status = 'completed'
                        AND DATE(o.order_date AT TIME ZONE 'America/Bogota') >= CURRENT_DATE - INTERVAL '7 days'
                    GROUP BY p.id, p.name
                    ORDER BY order_count DESC
                    LIMIT 3
                ),
                missing_ingredients AS (
                    SELECT
                        tp.id as product_id,
                        tp.name as product_name,
                        tp.order_count,
                        COUNT(DISTINCT pr.ingredient_id) as total_ingredients,
                        COUNT(DISTINCT CASE
                            WHEN ti.current_stock <= 0 THEN pr.ingredient_id
                            ELSE NULL
                        END) as out_of_stock_count,
                        STRING_AGG(DISTINCT
                            CASE
                                WHEN ti.current_stock <= 0 THEN i.name
                                ELSE NULL
                            END, ', '
                        ) as missing_list
                    FROM top_products tp
                    JOIN product_recipes pr ON tp.id = pr.product_id
                    JOIN ingredients i ON pr.ingredient_id = i.id
                    LEFT JOIN tenant_inventory ti ON ti.ingredient_id = i.id AND ti.tenant_id = $1
                    GROUP BY tp.id, tp.name, tp.order_count
                    HAVING COUNT(DISTINCT CASE WHEN ti.current_stock <= 0 THEN pr.ingredient_id ELSE NULL END) > 0
                )
                SELECT * FROM missing_ingredients
                ORDER BY order_count DESC
                LIMIT 2
            """
            blocked_rows = await conn.fetch(blocked_products_query, tenant_id)

            for row in blocked_rows:
                title = f"🔴 Producto bloqueado: {row['product_name']}"
                description = f"Vendido {row['order_count']} veces en 7 días. Faltan {row['out_of_stock_count']} ingredientes: {row['missing_list'][:80]}..."

                alerts.append({
                    "id": f"blocked_product_{len(alerts)}",
                    "type": "critical",
                    "title": title,
                    "description": description,
                    "action": {
                        "label": "Ver ingredientes",
                        "url": "/menu/productos"
                    }
                })

        # 3. Low stock with days remaining calculation
        if len(alerts) < limit:
            low_stock_smart_query = """
                WITH consumption_rate AS (
                    SELECT
                        tim.ingredient_id,
                        (SUM(ABS(tim.quantity_change)) / 7.0) as daily_rate
                    FROM tenant_ingredient_movements tim
                    WHERE tim.tenant_id = $1
                        AND tim.movement_type = 'consumption'
                        AND tim.created_at >= CURRENT_DATE - INTERVAL '7 days'
                    GROUP BY tim.ingredient_id
                    HAVING SUM(ABS(tim.quantity_change)) > 0
                )
                SELECT
                    i.name,
                    i.unit,
                    ti.current_stock,
                    cr.daily_rate,
                    (ti.current_stock / NULLIF(cr.daily_rate, 0)) as days_remaining,
                    CEIL(cr.daily_rate * 14) as recommended_order
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                JOIN consumption_rate cr ON cr.ingredient_id = i.id
                WHERE ti.tenant_id = $1
                    AND ti.current_stock > 0
                    AND (ti.current_stock / NULLIF(cr.daily_rate, 0)) < 10
                ORDER BY days_remaining ASC
                LIMIT 3
            """
            low_stock_rows = await conn.fetch(low_stock_smart_query, tenant_id)

            for row in low_stock_rows:
                days = float(row['days_remaining'])
                if days <= 3:
                    severity = "critical"
                    title = f"🔴 Crítico: {row['name']}"
                elif days <= 7:
                    severity = "warning"
                    title = f"🟡 Stock bajo: {row['name']}"
                else:
                    severity = "warning"
                    title = f"Stock limitado: {row['name']}"

                description = f"Quedan {days:.1f} días de stock ({row['current_stock']:.0f}{row['unit']}). Orden sugerida: {float(row['recommended_order']):.0f}{row['unit']}"

                alerts.append({
                    "id": f"low_stock_smart_{len(alerts)}",
                    "type": severity,
                    "title": title,
                    "description": description,
                    "action": {
                        "label": "Pedir ahora",
                        "url": "/abastecimiento/compras/crear"
                    }
                })

        # 4. Long gap without purchases (operational warning)
        if len(alerts) < limit:
            purchase_gap_query = """
                SELECT
                    MAX(created_at) as last_purchase_date,
                    CURRENT_DATE - MAX(created_at::date) as days_since_purchase,
                    COUNT(DISTINCT ingredient_id) as ingredients_purchased
                FROM tenant_ingredient_movements
                WHERE tenant_id = $1
                    AND movement_type = 'purchase'
            """
            gap_row = await conn.fetchrow(purchase_gap_query, tenant_id)

            if gap_row and gap_row['last_purchase_date']:
                days_gap = gap_row['days_since_purchase']
                if days_gap >= 14:
                    severity = "critical" if days_gap >= 21 else "warning"
                    emoji = "🔴" if days_gap >= 21 else "🟡"

                    title = f"{emoji} Sin compras: {days_gap} días"
                    description = f"Última compra: {gap_row['last_purchase_date'].strftime('%d/%m/%Y')}. Con POS activo, el stock se agota rápido. Programa una compra pronto."

                    alerts.append({
                        "id": f"purchase_gap_{len(alerts)}",
                        "type": severity,
                        "title": title,
                        "description": description,
                        "action": {
                            "label": "Crear compra",
                            "url": "/abastecimiento/compras/crear"
                        }
                    })

        # 5. Expiration warnings (next 7 days)
        if len(alerts) < limit:
            expiration_query = """
                SELECT
                    i.name,
                    ti.current_stock,
                    ti.fecha_vencimiento,
                    ti.lote_actual,
                    (ti.fecha_vencimiento - CURRENT_DATE) as days_until_expiry
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                WHERE ti.tenant_id = $1
                    AND ti.fecha_vencimiento IS NOT NULL
                    AND ti.fecha_vencimiento <= CURRENT_DATE + INTERVAL '7 days'
                    AND ti.current_stock > 0
                ORDER BY ti.fecha_vencimiento ASC
                LIMIT 2
            """
            expiration_rows = await conn.fetch(expiration_query, tenant_id)

            for row in expiration_rows:
                days = row['days_until_expiry']
                if days <= 0:
                    title = f"⚠️ Vencido: {row['name']}"
                    severity = "critical"
                elif days <= 2:
                    title = f"🔴 Vence en {days} días: {row['name']}"
                    severity = "critical"
                else:
                    title = f"🟡 Vence en {days} días: {row['name']}"
                    severity = "warning"

                description = f"Lote {row['lote_actual']} vence el {row['fecha_vencimiento'].strftime('%d/%m/%Y')}. Stock: {row['current_stock']}"

                alerts.append({
                    "id": f"expiry_{len(alerts)}",
                    "type": severity,
                    "title": title,
                    "description": description,
                    "action": {
                        "label": "Ver inventario",
                        "url": "/inventario"
                    }
                })

        # 6. Top selling products (informational)
        if len(alerts) < limit:
            top_products_query = """
                SELECT
                    p.name,
                    COUNT(DISTINCT oi.id) as order_count,
                    SUM(oi.quantity) as total_units
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.id
                JOIN product p ON oi.product_id = p.id
                WHERE o.tenant_id = $1
                    AND o.status = 'completed'
                    AND DATE(o.order_date AT TIME ZONE 'America/Bogota') >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY p.id, p.name
                ORDER BY order_count DESC
                LIMIT 1
            """
            top_row = await conn.fetchrow(top_products_query, tenant_id)

            if top_row:
                alerts.append({
                    "id": f"top_product_{len(alerts)}",
                    "type": "info",
                    "title": f"⭐ Producto estrella: {top_row['name']}",
                    "description": f"{top_row['order_count']} pedidos esta semana ({top_row['total_units']} unidades). Asegura stock de ingredientes.",
                    "action": {
                        "label": "Ver receta",
                        "url": "/menu/productos"
                    }
                })

        # Limit total alerts
        alerts = alerts[:limit]

        return {
            "success": True,
            "data": {
                "alerts": alerts,
                "total": len(alerts),
                "critical_count": sum(1 for a in alerts if a['type'] == 'critical'),
                "warning_count": sum(1 for a in alerts if a['type'] == 'warning'),
                "info_count": sum(1 for a in alerts if a['type'] == 'info')
            }
        }



async def get_alerts(
    request: Request,
    limit: int = 10
) -> dict:
    """
    Get intelligent system alerts for inventory, products, and operational issues

    Smart alerts include:
    - Out of stock ingredients with active consumption (POS-only operations)
    - Top selling products blocked by missing ingredients
    - Low stock warnings with days remaining
    - Expiration warnings
    - Long gaps without purchases
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")
        return await _get_alerts_for_tenant(tenant_id, limit)
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting alerts: {str(e)}")
        raise APIError(f"Error getting alerts: {str(e)}", status_code=500)


# ---------------------------------------------------------------------------
# Data Quality — anomaly detection helpers
# ---------------------------------------------------------------------------

def _compute_anomaly(
    history: List[float],
    new_price: float,
    ingredient_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Return anomaly metadata if new_price is anomalous vs history, else None.

    Algorithm:
    1. Impossible value check (price <= 0)
    2. % deviation from rolling average (>25% warning, >50% critical)
    3. IQR fence with k=2 (upgrades to critical if outside fence)

    Requires at least 2 history points to run deviation checks.
    """
    if new_price <= 0:
        return {
            "alert_type": "impossible_value",
            "severity": "critical",
            "expected_value": None,
            "actual_value": new_price,
            "deviation_pct": None,
            "rolling_avg": None,
        }

    if len(history) < 2:
        return None

    rolling_avg = st_mean(history)
    if rolling_avg == 0:
        return None

    deviation_pct = abs(new_price - rolling_avg) / rolling_avg * 100

    if deviation_pct > 50:
        severity = "critical"
    elif deviation_pct > 25:
        severity = "warning"
    else:
        severity = None

    # IQR fence (needs at least 4 points for meaningful quartiles)
    if len(history) >= 4:
        q1, _, q3 = quantiles(history, n=4, method="inclusive")
        iqr = q3 - q1
        lower = q1 - 2 * iqr
        upper = q3 + 2 * iqr
        if new_price < lower or new_price > upper:
            severity = "critical"

    if severity is None:
        return None

    alert_type = "price_spike" if new_price > rolling_avg else "price_drop"

    return {
        "alert_type": alert_type,
        "severity": severity,
        "expected_value": rolling_avg,
        "actual_value": new_price,
        "deviation_pct": round(deviation_pct, 2),
        "rolling_avg": rolling_avg,
    }


async def _get_data_quality_for_tenant(tenant_id: str) -> dict:
    """Auth-agnostic core for data quality. Called by session wrapper and public API."""
    async with get_db_connection(use_transaction=False) as conn:
        alerts_query = """
            SELECT
                dqa.id, dqa.tenant_id, dqa.purchase_item_id, dqa.ingredient_id,
                dqa.ingredient_name, dqa.alert_type, dqa.severity,
                dqa.expected_value, dqa.actual_value, dqa.deviation_pct, dqa.rolling_avg,
                dqa.context, dqa.resolved, dqa.resolved_by, dqa.resolved_at,
                dqa.resolution_note, dqa.original_value, dqa.corrected_value, dqa.created_at,
                tpi.purchase_id,
                tp.purchase_date,
                tp.purchase_number,
                ts.name AS supplier_name
            FROM data_quality_alerts dqa
            LEFT JOIN tenant_purchase_items tpi ON tpi.id = dqa.purchase_item_id
            LEFT JOIN tenant_purchases tp ON tp.id = tpi.purchase_id
            LEFT JOIN tenant_suppliers ts ON ts.id = tp.supplier_id
            WHERE dqa.tenant_id = $1
            ORDER BY
                dqa.resolved ASC,
                CASE dqa.severity WHEN 'critical' THEN 0 ELSE 1 END,
                dqa.created_at DESC
        """
        alert_rows = await conn.fetch(alerts_query, tenant_id)

        alerts = []
        for r in alert_rows:
            alerts.append({
                "id": str(r["id"]),
                "tenant_id": str(r["tenant_id"]),
                "purchase_item_id": str(r["purchase_item_id"]) if r["purchase_item_id"] else None,
                "purchase_id": str(r["purchase_id"]) if r["purchase_id"] else None,
                "purchase_date": r["purchase_date"].isoformat() if r["purchase_date"] else None,
                "purchase_number": r["purchase_number"],
                "supplier_name": r["supplier_name"],
                "ingredient_id": str(r["ingredient_id"]) if r["ingredient_id"] else None,
                "ingredient_name": r["ingredient_name"],
                "alert_type": r["alert_type"],
                "severity": r["severity"],
                "expected_value": float(r["expected_value"]) if r["expected_value"] is not None else None,
                "actual_value": float(r["actual_value"]) if r["actual_value"] is not None else None,
                "deviation_pct": float(r["deviation_pct"]) if r["deviation_pct"] is not None else None,
                "rolling_avg": float(r["rolling_avg"]) if r["rolling_avg"] is not None else None,
                "context": r["context"],
                "resolved": r["resolved"],
                "resolved_by": str(r["resolved_by"]) if r["resolved_by"] else None,
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                "resolution_note": r["resolution_note"],
                "original_value": float(r["original_value"]) if r["original_value"] is not None else None,
                "corrected_value": float(r["corrected_value"]) if r["corrected_value"] is not None else None,
                "created_at": r["created_at"].isoformat(),
            })

        critical_count = sum(1 for a in alerts if a["severity"] == "critical" and not a["resolved"])
        warning_count = sum(1 for a in alerts if a["severity"] == "warning" and not a["resolved"])
        resolved_count = sum(1 for a in alerts if a["resolved"])
        score = max(0, 100 - critical_count * 10 - warning_count * 2)

        return {
            "success": True,
            "data": {
                "score": score,
                "critical": critical_count,
                "warning": warning_count,
                "resolved": resolved_count,
                "alerts": alerts,
            },
        }



async def get_data_quality(request: Request) -> dict:
    """
    Returns existing alerts from data_quality_alerts plus a summary score.

    Read-only endpoint — anomaly detection and upserts happen in the background
    task run_anomaly_checks_for_purchase(), triggered after each purchase save.

    Score: max(0, 100 - critical*10 - warning*2)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")
        return await _get_data_quality_for_tenant(tenant_id)
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting data quality: {str(e)}")
        raise APIError(f"Error getting data quality: {str(e)}", status_code=500)


async def run_anomaly_checks_for_purchase(purchase_id: UUID, tenant_id: UUID) -> None:
    """
    Background task: scan each item of a newly saved purchase for price anomalies.

    Triggered automatically after POST /purchases/direct commits.
    Requires >= 5 prior purchases for the ingredient before running — below that
    there is not enough baseline to flag anomalies reliably.

    Upserts detected anomalies into data_quality_alerts idempotently via
    ON CONFLICT (purchase_item_id, tenant_id).
    """
    try:
        # Read-only: fetch items for this purchase + ingredient names
        async with get_db_connection(use_transaction=False) as conn:
            items = await conn.fetch("""
                SELECT
                    tpi.id          AS purchase_item_id,
                    tpi.ingredient_id,
                    i.name          AS ingredient_name,
                    tpi.unit_cost::float AS unit_cost
                FROM tenant_purchase_items tpi
                JOIN ingredients i ON tpi.ingredient_id = i.id
                WHERE tpi.purchase_id = $1
                  AND tpi.unit_cost IS NOT NULL
            """, purchase_id)

        if not items:
            logger.debug(f"No items found for purchase {purchase_id} — skipping anomaly check")
            return

        for item in items:
            try:
                ingredient_id = item["ingredient_id"]
                new_price = item["unit_cost"]
                ingredient_name = item["ingredient_name"]
                purchase_item_id = item["purchase_item_id"]

                # Fetch up to 30 prior prices for this ingredient, excluding the current purchase
                async with get_db_connection(use_transaction=False) as conn:
                    history_rows = await conn.fetch("""
                        SELECT tpi2.unit_cost::float AS price
                        FROM tenant_purchase_items tpi2
                        JOIN tenant_purchases tp ON tpi2.purchase_id = tp.id
                        WHERE tpi2.ingredient_id = $1
                          AND tp.tenant_id = $2
                          AND tpi2.purchase_id != $3
                          AND tpi2.unit_cost IS NOT NULL
                          AND tpi2.unit_cost > 0
                        ORDER BY tp.purchase_date DESC
                        LIMIT 30
                    """, ingredient_id, tenant_id, purchase_id)

                history: List[float] = [r["price"] for r in history_rows]

                # Issue spec: minimum 5 prior purchases required for a reliable baseline
                if len(history) < 5:
                    logger.debug(
                        f"Ingredient {ingredient_name}: only {len(history)} prior purchases "
                        f"(need >= 5) — skipping"
                    )
                    continue

                anomaly = _compute_anomaly(history, new_price, ingredient_name)
                if anomaly is None:
                    continue

                logger.info(
                    f"Anomaly detected: {ingredient_name} | "
                    f"{anomaly['alert_type']} | {anomaly['severity']} | "
                    f"actual={new_price} expected={anomaly['expected_value']:.2f} "
                    f"deviation={anomaly['deviation_pct']}%"
                )

                # Write: upsert alert
                async with get_db_connection() as conn:
                    await conn.execute("""
                        INSERT INTO data_quality_alerts (
                            tenant_id, purchase_item_id, ingredient_id,
                            ingredient_name, alert_type, severity,
                            expected_value, actual_value, deviation_pct, rolling_avg,
                            resolved
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, FALSE)
                        ON CONFLICT (purchase_item_id, tenant_id)
                        DO UPDATE SET
                            alert_type     = EXCLUDED.alert_type,
                            severity       = EXCLUDED.severity,
                            expected_value = EXCLUDED.expected_value,
                            actual_value   = EXCLUDED.actual_value,
                            deviation_pct  = EXCLUDED.deviation_pct,
                            rolling_avg    = EXCLUDED.rolling_avg
                        WHERE data_quality_alerts.resolved = FALSE
                    """,
                        tenant_id,
                        purchase_item_id,
                        ingredient_id,
                        ingredient_name,
                        anomaly["alert_type"],
                        anomaly["severity"],
                        anomaly["expected_value"],
                        anomaly["actual_value"],
                        anomaly["deviation_pct"],
                        anomaly["rolling_avg"],
                    )

            except Exception as item_err:
                logger.error(
                    f"Anomaly check failed for item {item.get('purchase_item_id')} "
                    f"({item.get('ingredient_name')}): {item_err}"
                )
                continue

    except Exception as e:
        logger.error(f"run_anomaly_checks_for_purchase failed (purchase={purchase_id}): {e}")


async def resolve_data_quality_alert(
    request: Request,
    alert_id: UUID,
    resolve_data: Any,  # DataQualityAlertResolve — typed as Any to avoid circular import
) -> dict:
    """
    Resolve a data quality alert in one of two modes:

    - ``valid``: marks the alert resolved with no changes to purchase data.
    - ``corrected``: updates ``unit_cost`` (and optionally ``purchase_quantity``)
      on ``tenant_purchase_items``, recalculates ``product.costo_calculado`` for
      all products that use the ingredient, then marks the alert resolved with a
      full audit trail (``original_value``, ``corrected_value``, ``resolved_by``,
      ``resolved_at``).

    A pre-resolution anomaly re-check runs on the corrected value before accepting
    it — if the new value would itself trigger an anomaly, a 400 is returned.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # 1. Fetch alert — tenant-scoped to prevent cross-tenant access
            alert = await conn.fetchrow("""
                SELECT
                    id, tenant_id, purchase_item_id, ingredient_id,
                    ingredient_name, alert_type, severity,
                    actual_value, resolved, resolution_note
                FROM data_quality_alerts
                WHERE id = $1 AND tenant_id = $2
            """, alert_id, tenant_id)

            if not alert:
                raise APIError("Alert not found", status_code=404)

            if alert["resolved"]:
                raise APIError("Alert already resolved", status_code=400)

            resolution_type = resolve_data.resolution_type
            resolution_note = resolve_data.resolution_note

            if resolution_type == "valid":
                # Mark resolved without touching purchase data
                await conn.execute("""
                    UPDATE data_quality_alerts
                    SET resolved = TRUE,
                        resolved_by = $2,
                        resolved_at = NOW(),
                        resolution_note = $3
                    WHERE id = $1
                """, alert_id, user_id, resolution_note)

            elif resolution_type == "corrected":
                corrected_value = resolve_data.corrected_value
                corrected_quantity = resolve_data.corrected_quantity

                if not corrected_value or corrected_value <= 0:
                    raise APIError(
                        "corrected_value is required and must be > 0 for resolution_type 'corrected'",
                        status_code=422
                    )

                purchase_item_id = alert["purchase_item_id"]
                ingredient_id = alert["ingredient_id"]
                ingredient_name = alert["ingredient_name"]

                if not purchase_item_id:
                    raise APIError(
                        "Cannot correct: alert has no associated purchase_item_id",
                        status_code=400
                    )

                # 2. Pre-resolution anomaly re-check on the corrected value
                if purchase_item_id:
                    purchase_row = await conn.fetchrow("""
                        SELECT tpi.purchase_id FROM tenant_purchase_items tpi WHERE tpi.id = $1
                    """, purchase_item_id)

                    if purchase_row:
                        history_rows = await conn.fetch("""
                            SELECT tpi2.unit_cost::float AS price
                            FROM tenant_purchase_items tpi2
                            JOIN tenant_purchases tp ON tpi2.purchase_id = tp.id
                            WHERE tpi2.ingredient_id = $1
                              AND tp.tenant_id = $2
                              AND tpi2.purchase_id != $3
                              AND tpi2.unit_cost IS NOT NULL
                              AND tpi2.unit_cost > 0
                            ORDER BY tp.purchase_date DESC
                            LIMIT 30
                        """, ingredient_id, tenant_id, purchase_row["purchase_id"])

                        history: List[float] = [r["price"] for r in history_rows]

                        if len(history) >= 5:
                            recheck = _compute_anomaly(history, corrected_value, ingredient_name)
                            if recheck is not None:
                                raise APIError(
                                    f"Corrected value ${corrected_value:,.2f} is also anomalous "
                                    f"({recheck['alert_type']}, {recheck['severity']}, "
                                    f"{recheck['deviation_pct']}% deviation from avg "
                                    f"${recheck['expected_value']:,.2f}). "
                                    f"Provide a value closer to the historical average.",
                                    status_code=400
                                )

                # 3. Fetch current purchase_quantity for total_cost calculation
                item_row = await conn.fetchrow("""
                    SELECT purchase_quantity FROM tenant_purchase_items WHERE id = $1
                """, purchase_item_id)

                effective_quantity = (
                    corrected_quantity
                    if corrected_quantity is not None
                    else (float(item_row["purchase_quantity"]) if item_row and item_row["purchase_quantity"] else 1.0)
                )

                # 4. Update tenant_purchase_items
                await conn.execute("""
                    UPDATE tenant_purchase_items
                    SET unit_cost       = $2,
                        purchase_quantity = $3,
                        total_cost      = $4
                    WHERE id = $1
                """,
                    purchase_item_id,
                    corrected_value,
                    effective_quantity,
                    effective_quantity * corrected_value,
                )

                # 5. Recalculate costo_calculado for all products using this ingredient
                await conn.execute("""
                    UPDATE product
                    SET costo_calculado = (
                        SELECT COALESCE(SUM(
                            pr.quantity * COALESCE(
                                (SELECT pi.unit_cost
                                 FROM tenant_purchase_items pi
                                 JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                                 WHERE pi.ingredient_id = pr.ingredient_id
                                   AND tp.tenant_id = $2
                                   AND pi.unit_cost IS NOT NULL AND pi.unit_cost > 0
                                 ORDER BY tp.purchase_date DESC LIMIT 1),
                                i.costo_unitario, 0
                            )
                        ), 0)
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = product.id
                    )
                    WHERE id IN (
                        SELECT DISTINCT pr.product_id
                        FROM product_recipes pr
                        WHERE pr.ingredient_id = $1
                    )
                """, ingredient_id, tenant_id)

                # 6. Mark alert resolved with full audit trail
                await conn.execute("""
                    UPDATE data_quality_alerts
                    SET resolved        = TRUE,
                        resolved_by     = $2,
                        resolved_at     = NOW(),
                        resolution_note = $3,
                        original_value  = $4,
                        corrected_value = $5
                    WHERE id = $1
                """,
                    alert_id,
                    user_id,
                    resolution_note,
                    alert["actual_value"],
                    corrected_value,
                )

            else:
                raise APIError(
                    f"Invalid resolution_type '{resolution_type}'. Use 'valid' or 'corrected'.",
                    status_code=422
                )

            # Return the updated alert
            updated = await conn.fetchrow("""
                SELECT
                    id, tenant_id, purchase_item_id, ingredient_id,
                    ingredient_name, alert_type, severity,
                    expected_value, actual_value, deviation_pct, rolling_avg,
                    context, resolved, resolved_by, resolved_at,
                    resolution_note, original_value, corrected_value, created_at
                FROM data_quality_alerts
                WHERE id = $1
            """, alert_id)

            return {
                "success": True,
                "data": {
                    "id": str(updated["id"]),
                    "tenant_id": str(updated["tenant_id"]),
                    "purchase_item_id": str(updated["purchase_item_id"]) if updated["purchase_item_id"] else None,
                    "ingredient_id": str(updated["ingredient_id"]) if updated["ingredient_id"] else None,
                    "ingredient_name": updated["ingredient_name"],
                    "alert_type": updated["alert_type"],
                    "severity": updated["severity"],
                    "expected_value": float(updated["expected_value"]) if updated["expected_value"] is not None else None,
                    "actual_value": float(updated["actual_value"]) if updated["actual_value"] is not None else None,
                    "deviation_pct": float(updated["deviation_pct"]) if updated["deviation_pct"] is not None else None,
                    "rolling_avg": float(updated["rolling_avg"]) if updated["rolling_avg"] is not None else None,
                    "resolved": updated["resolved"],
                    "resolved_by": str(updated["resolved_by"]) if updated["resolved_by"] else None,
                    "resolved_at": updated["resolved_at"].isoformat() if updated["resolved_at"] else None,
                    "resolution_note": updated["resolution_note"],
                    "original_value": float(updated["original_value"]) if updated["original_value"] is not None else None,
                    "corrected_value": float(updated["corrected_value"]) if updated["corrected_value"] is not None else None,
                    "created_at": updated["created_at"].isoformat(),
                },
            }

    except AuthenticationError as e:
        raise e
    except APIError as e:
        raise e
    except Exception as e:
        logger.error(f"Error resolving data quality alert {alert_id}: {str(e)}")
        raise APIError(f"Error resolving alert: {str(e)}", status_code=500)


async def _get_cohort_for_tenant(
    tenant_id: str,
    period: str = "weekly",
    periods: int = 8,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timezone: str = "America/Bogota",
) -> dict:
    """
    Auth-agnostic cohort retention analysis.

    Builds a retention matrix: rows = acquisition cohort (week/month of first order),
    columns = periods since first visit (1..N), cells = % of cohort that returned.

    Only identified customers (customer_id IS NOT NULL) are included.
    All order sources (POS, online, manual) count as a visit.
    A customer with multiple orders in the same period counts once (DISTINCT).
    """
    if period not in ("weekly", "monthly"):
        raise APIError("period must be 'weekly' or 'monthly'", status_code=400)

    today = datetime.now().date()

    parsed_date_from = parse_date(date_from)
    parsed_date_to = parse_date(date_to)

    if not parsed_date_from:
        parsed_date_from = today - timedelta(days=90 if period == "weekly" else 365)
    if not parsed_date_to:
        parsed_date_to = today

    trunc_unit = "week" if period == "weekly" else "month"

    # Timezone string is embedded as a literal — same pattern used across analytics endpoints.
    # It is not user-controlled free-text; it comes from a validated Pydantic field with a
    # fixed default. PostgreSQL will raise an error on an invalid timezone name.

    if period == "weekly":
        query = f"""
            WITH first_orders AS (
                SELECT
                    customer_id,
                    DATE_TRUNC('{trunc_unit}',
                        MIN(order_date) AT TIME ZONE '{timezone}') AS cohort_period
                FROM orders
                WHERE customer_id IS NOT NULL
                  AND tenant_id = $1
                  AND order_date >= $2::date
                  AND order_date < $3::date + interval '1 day'
                GROUP BY customer_id
            ),
            cohort_sizes AS (
                SELECT cohort_period, COUNT(*) AS cohort_size
                FROM first_orders
                GROUP BY cohort_period
            ),
            customer_active_periods AS (
                SELECT DISTINCT
                    fo.customer_id,
                    fo.cohort_period,
                    DATE_TRUNC('{trunc_unit}',
                        o.order_date AT TIME ZONE '{timezone}') AS active_period
                FROM orders o
                JOIN first_orders fo ON fo.customer_id = o.customer_id
                WHERE o.tenant_id = $1
            )
            SELECT
                fo.cohort_period,
                cs.cohort_size,
                (EXTRACT(EPOCH FROM (caw.active_period - fo.cohort_period))
                 / 604800)::int                              AS period_offset,
                COUNT(DISTINCT fo.customer_id)               AS returning_count,
                ROUND(
                    100.0 * COUNT(DISTINCT fo.customer_id)
                    / cs.cohort_size,
                1)                                           AS retention_pct
            FROM customer_active_periods caw
            JOIN first_orders fo
                ON fo.customer_id = caw.customer_id
               AND fo.cohort_period = caw.cohort_period
            JOIN cohort_sizes cs ON cs.cohort_period = fo.cohort_period
            WHERE (EXTRACT(EPOCH FROM (caw.active_period - fo.cohort_period))
                   / 604800)::int BETWEEN 1 AND $4
            GROUP BY fo.cohort_period, cs.cohort_size, period_offset
            ORDER BY fo.cohort_period DESC, period_offset
        """
    else:
        query = f"""
            WITH first_orders AS (
                SELECT
                    customer_id,
                    DATE_TRUNC('{trunc_unit}',
                        MIN(order_date) AT TIME ZONE '{timezone}') AS cohort_period
                FROM orders
                WHERE customer_id IS NOT NULL
                  AND tenant_id = $1
                  AND order_date >= $2::date
                  AND order_date < $3::date + interval '1 day'
                GROUP BY customer_id
            ),
            cohort_sizes AS (
                SELECT cohort_period, COUNT(*) AS cohort_size
                FROM first_orders
                GROUP BY cohort_period
            ),
            customer_active_periods AS (
                SELECT DISTINCT
                    fo.customer_id,
                    fo.cohort_period,
                    DATE_TRUNC('{trunc_unit}',
                        o.order_date AT TIME ZONE '{timezone}') AS active_period
                FROM orders o
                JOIN first_orders fo ON fo.customer_id = o.customer_id
                WHERE o.tenant_id = $1
            )
            SELECT
                fo.cohort_period,
                cs.cohort_size,
                (EXTRACT(YEAR FROM AGE(caw.active_period, fo.cohort_period)) * 12
                 + EXTRACT(MONTH FROM AGE(caw.active_period, fo.cohort_period)))::int
                                                             AS period_offset,
                COUNT(DISTINCT fo.customer_id)               AS returning_count,
                ROUND(
                    100.0 * COUNT(DISTINCT fo.customer_id)
                    / cs.cohort_size,
                1)                                           AS retention_pct
            FROM customer_active_periods caw
            JOIN first_orders fo
                ON fo.customer_id = caw.customer_id
               AND fo.cohort_period = caw.cohort_period
            JOIN cohort_sizes cs ON cs.cohort_period = fo.cohort_period
            WHERE (EXTRACT(YEAR FROM AGE(caw.active_period, fo.cohort_period)) * 12
                   + EXTRACT(MONTH FROM AGE(caw.active_period, fo.cohort_period)))::int
                  BETWEEN 1 AND $4
            GROUP BY fo.cohort_period, cs.cohort_size, period_offset
            ORDER BY fo.cohort_period DESC, period_offset
        """

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            query,
            UUID(tenant_id),
            parsed_date_from,
            parsed_date_to,
            periods,
        )

    # Group rows by cohort_period and build the full retention matrix.
    # SQL only returns periods with returning_count > 0; fill gaps with zeros.
    cohort_map: Dict[Any, dict] = {}
    for row in rows:
        key = row['cohort_period']
        if key not in cohort_map:
            cohort_date = key.date() if hasattr(key, 'date') else key
            if period == "weekly":
                iso = cohort_date.isocalendar()
                label = f"{iso[0]}-W{iso[1]:02d}"
            else:
                label = cohort_date.strftime("%Y-%m")

            cohort_map[key] = {
                "cohort_label": label,
                "cohort_date": cohort_date.isoformat(),
                "cohort_size": int(row['cohort_size']),
                "_retention_map": {},
            }
        cohort_map[key]["_retention_map"][int(row['period_offset'])] = {
            "period": int(row['period_offset']),
            "count": int(row['returning_count']),
            "pct": float(row['retention_pct']),
        }

    cohorts = []
    for cohort in cohort_map.values():
        retention_map = cohort.pop("_retention_map")
        retention = [
            retention_map.get(p, {"period": p, "count": 0, "pct": 0.0})
            for p in range(1, periods + 1)
        ]
        cohort["retention"] = retention
        cohorts.append(cohort)

    return {
        "period": period,
        "cohorts": cohorts,
    }


async def _get_rfm_for_tenant(
    tenant_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    segments: int = 5,
    timezone: str = "America/Bogota",
) -> dict:
    """
    Auth-agnostic RFM customer segmentation.

    Scores each identified, non-anonymous customer on:
      R (Recency)   — NTILE over days since last order (lower days = higher score)
      F (Frequency) — NTILE over order count (higher count = higher score)
      M (Monetary)  — NTILE over total spent (higher spend = higher score)

    Segment labels are assigned top-to-bottom (first match wins):
      Champions   — top tier on all three dimensions
      Loyal       — high frequency + monetary (any recency)
      At Risk     — low recency, but previously high frequency + monetary
      Hibernating — low recency and low frequency
      Lost        — catch-all for remaining combinations
    """
    today = datetime.now().date()

    parsed_date_from = parse_date(date_from)
    parsed_date_to = parse_date(date_to)

    if not parsed_date_from:
        parsed_date_from = today.replace(month=1, day=1)
    if not parsed_date_to:
        parsed_date_to = today

    # Thresholds scaled to the configured number of segments.
    # For segments=5: high_threshold=4, mid_threshold=3, low_threshold=2
    # For segments=3: high_threshold=3, mid_threshold=2, low_threshold=1
    high_threshold = max(1, int(segments * 0.7 + 0.5))   # ceil(segments*0.7)
    mid_threshold = max(1, int(segments * 0.6 + 0.5))    # ceil(segments*0.6)
    low_threshold = max(1, int(segments * 0.4))           # floor(segments*0.4)

    # Timezone is embedded as a literal — same safe pattern used across all analytics
    # functions. It comes from a validated Pydantic field; PostgreSQL raises on invalid tz.
    query = f"""
        WITH base AS (
            SELECT
                o.customer_id,
                p.name                                                     AS customer_name,
                COUNT(*)::int                                              AS order_count,
                MAX(o.order_date AT TIME ZONE '{timezone}')                AS last_order_date,
                SUM(o.total_amount)::bigint                                AS total_spent,
                EXTRACT(
                    EPOCH FROM (
                        NOW() AT TIME ZONE '{timezone}'
                        - MAX(o.order_date AT TIME ZONE '{timezone}')
                    )
                ) / 86400.0                                                AS recency_days
            FROM orders o
            JOIN profile p ON p.id = o.customer_id
            WHERE o.tenant_id   = $1
              AND o.status      = 'completed'
              AND o.customer_id IS NOT NULL
              AND p.email NOT LIKE '%@customer.temp'
              AND DATE(o.order_date AT TIME ZONE '{timezone}') >= $2
              AND DATE(o.order_date AT TIME ZONE '{timezone}') <= $3
            GROUP BY o.customer_id, p.name
        ),
        scored AS (
            SELECT
                customer_id,
                customer_name,
                order_count,
                last_order_date,
                total_spent,
                NTILE($4) OVER (ORDER BY recency_days ASC)  AS r_score,
                NTILE($4) OVER (ORDER BY order_count DESC)  AS f_score,
                NTILE($4) OVER (ORDER BY total_spent DESC)  AS m_score
            FROM base
        )
        SELECT
            customer_id::text,
            customer_name,
            order_count,
            last_order_date,
            total_spent,
            r_score,
            f_score,
            m_score,
            CASE
                WHEN r_score >= $5 AND f_score >= $5 AND m_score >= $5 THEN 'Champions'
                WHEN f_score >= $5 AND m_score >= $6                    THEN 'Loyal'
                WHEN r_score <= $7 AND f_score >= $6 AND m_score >= $6  THEN 'At Risk'
                WHEN r_score <= $7 AND f_score <= $7                    THEN 'Hibernating'
                ELSE 'Lost'
            END AS segment
        FROM scored
        ORDER BY r_score DESC, f_score DESC, m_score DESC
    """

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            query,
            tenant_id,
            parsed_date_from,
            parsed_date_to,
            segments,
            high_threshold,
            mid_threshold,
            low_threshold,
        )

    customers = [
        {
            "customer_id": row["customer_id"],
            "customer_name": row["customer_name"],
            "r_score": row["r_score"],
            "f_score": row["f_score"],
            "m_score": row["m_score"],
            "segment": row["segment"],
            "last_order_date": row["last_order_date"].isoformat() if row["last_order_date"] else None,
            "order_count": row["order_count"],
            "total_spent": row["total_spent"],
        }
        for row in rows
    ]

    return {
        "success": True,
        "data": {
            "customers": customers,
            "total": len(customers),
            "segments_used": segments,
            "evaluated_from": parsed_date_from.isoformat(),
            "evaluated_to": parsed_date_to.isoformat(),
        },
    }


async def _get_churn_risk_for_tenant(
    tenant_id: str,
    threshold_multiplier: float,
    min_orders: int,
    limit: int,
    offset: int,
) -> dict:
    """
    Returns identified customers whose inactivity exceeds threshold_multiplier × their
    personal average visit interval, sorted by lifetime_value DESC.

    Algorithm:
    - CTE 1 (intervals): LAG(order_date) per customer — required first level
    - CTE 2 (stats): aggregate COUNT, MAX, SUM, AVG — second level to avoid
      "aggregate function calls cannot contain window function calls" PostgreSQL error
    - Final SELECT: JOIN profile + waros_wallets, filter anonymous + above threshold

    risk_score formula: LEAST(1.0, (days_since / (multiplier × avg_interval) - 1.0) / 2.0)
      0.0 = just crossed threshold, 0.5 = 2× threshold, 1.0 = 3× threshold (capped)
    """
    async with get_db_connection(use_transaction=False) as conn:
        # COUNT for pagination (same filters, no LIMIT/OFFSET)
        count_row = await conn.fetchrow(
            """
            WITH intervals AS (
                SELECT customer_id, order_date,
                       LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS prev_date
                FROM orders
                WHERE status = 'completed'
                  AND customer_id IS NOT NULL
                  AND tenant_id = $1
            ),
            stats AS (
                SELECT i.customer_id,
                       COUNT(*)                                            AS order_count,
                       MAX(i.order_date)                                   AS last_order_date,
                       ROUND(AVG(
                           EXTRACT(EPOCH FROM (i.order_date - i.prev_date)) / 86400
                       )::numeric, 1)                                      AS avg_interval_days
                FROM intervals i
                JOIN orders o ON o.customer_id = i.customer_id
                             AND o.order_date  = i.order_date
                             AND o.status      = 'completed'
                             AND o.tenant_id   = $1
                GROUP BY i.customer_id
                HAVING COUNT(*) >= $2
            )
            SELECT COUNT(*) AS total
            FROM stats s
            JOIN profile p ON p.id = s.customer_id
            WHERE p.email NOT LIKE '%@customer.temp'
              AND s.avg_interval_days > 0
              AND EXTRACT(EPOCH FROM (NOW() - s.last_order_date)) / 86400
                  > ($3::numeric * s.avg_interval_days)
            """,
            tenant_id,
            min_orders,
            threshold_multiplier,
        )
        total_count = int(count_row["total"]) if count_row else 0

        rows = await conn.fetch(
            """
            WITH intervals AS (
                SELECT customer_id, order_date,
                       LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS prev_date
                FROM orders
                WHERE status = 'completed'
                  AND customer_id IS NOT NULL
                  AND tenant_id = $1
            ),
            stats AS (
                SELECT i.customer_id,
                       COUNT(*)                                            AS order_count,
                       MAX(i.order_date)                                   AS last_order_date,
                       SUM(o.total_amount)                                 AS lifetime_value,
                       ROUND(AVG(
                           EXTRACT(EPOCH FROM (i.order_date - i.prev_date)) / 86400
                       )::numeric, 1)                                      AS avg_interval_days
                FROM intervals i
                JOIN orders o ON o.customer_id = i.customer_id
                             AND o.order_date  = i.order_date
                             AND o.status      = 'completed'
                             AND o.tenant_id   = $1
                GROUP BY i.customer_id
                HAVING COUNT(*) >= $2
            )
            SELECT
                s.customer_id,
                p.name,
                p.phone_number,
                s.last_order_date,
                s.avg_interval_days,
                s.lifetime_value,
                ROUND(EXTRACT(EPOCH FROM (NOW() - s.last_order_date)) / 86400)::int
                    AS days_since_last_order,
                ROUND(
                    LEAST(1.0,
                        (EXTRACT(EPOCH FROM (NOW() - s.last_order_date)) / 86400
                         / ($3::numeric * s.avg_interval_days) - 1.0) / 2.0
                    )::numeric, 2
                )   AS risk_score,
                COALESCE(ww.current_balance, 0) AS waros_balance
            FROM stats s
            JOIN profile p ON p.id = s.customer_id
            LEFT JOIN waros_wallets ww ON ww.profile_id = s.customer_id
            WHERE p.email NOT LIKE '%@customer.temp'
              AND s.avg_interval_days > 0
              AND EXTRACT(EPOCH FROM (NOW() - s.last_order_date)) / 86400
                  > ($3::numeric * s.avg_interval_days)
            ORDER BY s.lifetime_value DESC, risk_score DESC
            LIMIT $4 OFFSET $5
            """,
            tenant_id,
            min_orders,
            threshold_multiplier,
            limit,
            offset,
        )

    customers = [
        {
            "customer_id": str(row["customer_id"]),
            "name": row["name"],
            "phone": row["phone_number"],
            "last_order_date": row["last_order_date"].isoformat(),
            "avg_visit_interval_days": float(row["avg_interval_days"]),
            "days_since_last_order": int(row["days_since_last_order"]),
            "risk_score": float(row["risk_score"]),
            "lifetime_value": int(row["lifetime_value"]),
            "waros_balance": int(row["waros_balance"]),
        }
        for row in rows
    ]

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "threshold_multiplier": threshold_multiplier,
        "min_orders": min_orders,
        "customers": customers,
    }

async def get_kitchen_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> dict:
    """
    Get kitchen performance metrics: avg prep time, station load, and delays.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        parsed_date_from = parse_date(date_from)
        parsed_date_to = parse_date(date_to)

        if not parsed_date_from or not parsed_date_to:
            today = datetime.now().date()
            parsed_date_to = today
            parsed_date_from = today - timedelta(days=7)

        async with get_db_connection() as conn:
            # 1. Summary Metrics & Station Breakdown
            # Uses kitchen_stations left join to include stations even if they had no orders in period
            query_stations = """
                WITH station_stats AS (
                    SELECT 
                        c.station_id,
                        COUNT(c.id) as total_orders,
                        AVG(EXTRACT(EPOCH FROM (c.ready_at - c.fired_at)) / 60) as avg_prep_min,
                        COUNT(CASE 
                            WHEN EXTRACT(EPOCH FROM (c.ready_at - c.fired_at)) / 60 > ks.alert_threshold_2_min 
                            THEN 1 END) as late_orders
                    FROM comandas c
                    JOIN kitchen_stations ks ON c.station_id = ks.id
                    WHERE c.tenant_id = $1
                        AND DATE(c.fired_at AT TIME ZONE 'America/Bogota') >= $2
                        AND DATE(c.fired_at AT TIME ZONE 'America/Bogota') <= $3
                        AND c.status IN ('ready', 'delivered')
                        AND c.ready_at IS NOT NULL
                    GROUP BY c.station_id, ks.alert_threshold_2_min
                )
                SELECT 
                    ks.id,
                    ks.name,
                    ks.color,
                    COALESCE(ss.total_orders, 0) as total_orders,
                    COALESCE(ss.avg_prep_min, 0) as avg_prep_min,
                    COALESCE(ss.late_orders, 0) as late_orders
                FROM kitchen_stations ks
                LEFT JOIN station_stats ss ON ks.id = ss.station_id
                WHERE ks.tenant_id = $1 AND ks.is_active = true
                ORDER BY ks.display_order
            """
            
            station_rows = await conn.fetch(query_stations, tenant_id, parsed_date_from, parsed_date_to)
            
            station_metrics = []
            total_orders = 0
            total_prep_sum = 0
            late_orders_count = 0
            
            for row in station_rows:
                total_orders += row['total_orders']
                total_prep_sum += (row['avg_prep_min'] * row['total_orders'])
                late_orders_count += row['late_orders']
                
                station_metrics.append({
                    "id": str(row['id']),
                    "name": row['name'],
                    "color": row['color'],
                    "total_orders": row['total_orders'],
                    "avg_prep_min": round(float(row['avg_prep_min']), 1),
                    "late_orders": row['late_orders'],
                    "efficiency_pct": round((1 - (row['late_orders'] / max(1, row['total_orders']))) * 100, 1)
                })

            avg_prep_time = round(total_prep_sum / max(1, total_orders), 1)

            # 2. Daily/Hourly Volume (Peak Hours)
            query_volume = """
                SELECT 
                    EXTRACT(HOUR FROM fired_at AT TIME ZONE 'America/Bogota')::int as hour,
                    COUNT(id) as count
                FROM comandas
                WHERE tenant_id = $1
                    AND DATE(fired_at AT TIME ZONE 'America/Bogota') >= $2
                    AND DATE(fired_at AT TIME ZONE 'America/Bogota') <= $3
                GROUP BY hour
                ORDER BY hour
            """
            volume_rows = await conn.fetch(query_volume, tenant_id, parsed_date_from, parsed_date_to)
            volume_by_hour = {row['hour']: row['count'] for row in volume_rows}
            
            # Fill missing hours
            peak_hours = []
            for h in range(24):
                peak_hours.append({"hour": h, "orders": volume_by_hour.get(h, 0)})

            # 3. Slowest Products (Top 10)
            query_products = """
                SELECT 
                    ci.kitchen_name as name,
                    COUNT(ci.id) as total_qty,
                    AVG(EXTRACT(EPOCH FROM (ci.ready_at - c.fired_at)) / 60) as avg_prep_min
                FROM comanda_items ci
                JOIN comandas c ON ci.comanda_id = c.id
                WHERE c.tenant_id = $1
                    AND DATE(c.fired_at AT TIME ZONE 'America/Bogota') >= $2
                    AND DATE(c.fired_at AT TIME ZONE 'America/Bogota') <= $3
                    AND ci.status = 'ready'
                    AND ci.ready_at IS NOT NULL
                GROUP BY ci.kitchen_name
                ORDER BY avg_prep_min DESC
                LIMIT 10
            """
            product_rows = await conn.fetch(query_products, tenant_id, parsed_date_from, parsed_date_to)
            slowest_products = [{
                "name": r['name'],
                "total_qty": float(r['total_qty']),
                "avg_prep_min": round(float(r['avg_prep_min']), 1)
            } for r in product_rows]

            return {
                "success": True,
                "data": {
                    "summary": {
                        "total_orders": total_orders,
                        "avg_prep_min": avg_prep_time,
                        "late_orders": late_orders_count,
                        "late_pct": round((late_orders_count / max(1, total_orders)) * 100, 1)
                    },
                    "stations": station_metrics,
                    "peak_hours": peak_hours,
                    "slowest_products": slowest_products,
                    "period": {
                        "from": parsed_date_from.isoformat(),
                        "to": parsed_date_to.isoformat()
                    }
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting kitchen metrics: {str(e)}")
        raise APIError(f"Error getting kitchen metrics: {str(e)}", status_code=500)
