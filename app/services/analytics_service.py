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
                        SUM(oi.subtotal) as total_revenue,
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

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting menu analysis: {str(e)}")
        raise APIError(f"Error getting menu analysis: {str(e)}", status_code=500)


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

        # Default to start of current year
        parsed_date_from = parse_date(date_from)
        parsed_date_to = parse_date(date_to)

        if not parsed_date_from or not parsed_date_to:
            today = datetime.now().date()
            parsed_date_to = today
            parsed_date_from = today.replace(month=1, day=1)

        # Calculate previous period (same duration)
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
                        SUM(oi.subtotal) as revenue,
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

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting food cost: {str(e)}")
        raise APIError(f"Error getting food cost: {str(e)}", status_code=500)


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


async def get_data_quality(request: Request) -> dict:
    """
    Scan the 30-day purchase history for each ingredient and detect price
    anomalies (spikes, drops, impossible values).

    Returns the existing alerts stored in data_quality_alerts plus a summary
    score.  New anomalies found on this run are upserted idempotently.

    Score: max(0, 100 - critical*10 - warning*2)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Ensure UNIQUE constraint exists for idempotent upsert
            await conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'uq_dqa_item_tenant'
                    ) THEN
                        ALTER TABLE data_quality_alerts
                        ADD CONSTRAINT uq_dqa_item_tenant
                        UNIQUE (purchase_item_id, tenant_id);
                    END IF;
                END
                $$;
            """)

            # Fetch last 30 days of purchase items with price history per ingredient
            history_query = """
                WITH recent_items AS (
                    SELECT
                        tpi.id            AS purchase_item_id,
                        tpi.ingredient_id,
                        i.name            AS ingredient_name,
                        tpi.unit_cost     AS price,
                        tp.purchase_date
                    FROM tenant_purchase_items tpi
                    JOIN tenant_purchases tp ON tpi.purchase_id = tp.id
                    JOIN ingredients i ON tpi.ingredient_id = i.id
                    WHERE tp.tenant_id = $1
                      AND tp.purchase_date >= CURRENT_DATE - INTERVAL '30 days'
                      AND tpi.unit_cost IS NOT NULL
                      AND tpi.unit_cost > 0
                    ORDER BY tpi.ingredient_id, tp.purchase_date ASC
                )
                SELECT
                    ingredient_id,
                    ingredient_name,
                    ARRAY_AGG(purchase_item_id ORDER BY purchase_date ASC) AS item_ids,
                    ARRAY_AGG(price::float ORDER BY purchase_date ASC)     AS prices
                FROM recent_items
                GROUP BY ingredient_id, ingredient_name
                HAVING COUNT(*) >= 2
            """
            rows = await conn.fetch(history_query, tenant_id)

            # Detect anomalies and upsert alerts
            for row in rows:
                prices: List[float] = list(row["prices"])
                item_ids = list(row["item_ids"])
                ingredient_id = row["ingredient_id"]
                ingredient_name = row["ingredient_name"]

                # Evaluate each item against its preceding history
                for idx in range(1, len(prices)):
                    history = prices[:idx]
                    new_price = prices[idx]
                    purchase_item_id = item_ids[idx]

                    anomaly = _compute_anomaly(history, new_price, ingredient_name)
                    if anomaly is None:
                        continue

                    await conn.execute("""
                        INSERT INTO data_quality_alerts (
                            tenant_id, purchase_item_id, ingredient_id,
                            ingredient_name, alert_type, severity,
                            expected_value, actual_value, deviation_pct, rolling_avg,
                            resolved
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, FALSE)
                        ON CONFLICT (purchase_item_id, tenant_id)
                        DO UPDATE SET
                            alert_type    = EXCLUDED.alert_type,
                            severity      = EXCLUDED.severity,
                            expected_value = EXCLUDED.expected_value,
                            actual_value  = EXCLUDED.actual_value,
                            deviation_pct = EXCLUDED.deviation_pct,
                            rolling_avg   = EXCLUDED.rolling_avg
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

            # Fetch all alerts for this tenant
            alerts_query = """
                SELECT
                    id, tenant_id, purchase_item_id, ingredient_id,
                    ingredient_name, alert_type, severity,
                    expected_value, actual_value, deviation_pct, rolling_avg,
                    context, resolved, resolved_by, resolved_at,
                    resolution_note, original_value, corrected_value, created_at
                FROM data_quality_alerts
                WHERE tenant_id = $1
                ORDER BY
                    resolved ASC,
                    CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                    created_at DESC
            """
            alert_rows = await conn.fetch(alerts_query, tenant_id)

            alerts = []
            for r in alert_rows:
                alerts.append({
                    "id": str(r["id"]),
                    "tenant_id": str(r["tenant_id"]),
                    "purchase_item_id": str(r["purchase_item_id"]) if r["purchase_item_id"] else None,
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
