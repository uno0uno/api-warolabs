"""
Analytics Service
Provides menu analysis, food cost tracking, and system alerts
"""
from typing import Optional
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from datetime import datetime, date, timedelta
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

        # Default to last 30 days
        parsed_date_from = parse_date(date_from)
        parsed_date_to = parse_date(date_to)

        if not parsed_date_from or not parsed_date_to:
            today = datetime.now().date()
            parsed_date_to = today
            parsed_date_from = today - timedelta(days=30)

        async with get_db_connection() as conn:
            # Get product sales with estimated profitability
            # Since costo_calculado is 0, we estimate cost at 40% of price (industry standard)
            query = """
                WITH product_sales AS (
                    SELECT
                        p.id,
                        p.name,
                        p.price,
                        COALESCE(p.costo_calculado, p.price * 0.40) as estimated_cost,
                        c.name as category_name,
                        COUNT(DISTINCT oi.id) as order_count,
                        SUM(oi.quantity) as total_units_sold,
                        SUM(oi.subtotal) as total_revenue,
                        AVG(oi.price_at_purchase) as avg_price
                    FROM product p
                    LEFT JOIN categories c ON p.category_id = c.id
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
                    GROUP BY p.id, p.name, p.price, p.costo_calculado, c.name
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

        # Default to current month
        parsed_date_from = parse_date(date_from)
        parsed_date_to = parse_date(date_to)

        if not parsed_date_from or not parsed_date_to:
            today = datetime.now().date()
            parsed_date_to = today
            # First day of current month
            parsed_date_from = today.replace(day=1)

        # Calculate previous period (same duration)
        days_diff = (parsed_date_to - parsed_date_from).days + 1
        prev_date_to = parsed_date_from - timedelta(days=1)
        prev_date_from = prev_date_to - timedelta(days=days_diff - 1)

        async with get_db_connection() as conn:
            # Query for current and previous period
            query = """
                WITH period_costs AS (
                    SELECT
                        SUM(oi.subtotal) as revenue,
                        SUM(oi.quantity * COALESCE(p.costo_calculado, p.price * 0.40)) as total_cost,
                        CASE
                            WHEN DATE(o.order_date AT TIME ZONE 'America/Bogota') >= $2
                                AND DATE(o.order_date AT TIME ZONE 'America/Bogota') <= $3
                            THEN 'current'
                            ELSE 'previous'
                        END as period
                    FROM order_items oi
                    JOIN orders o ON oi.order_id = o.id
                    JOIN product p ON oi.product_id = p.id
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
