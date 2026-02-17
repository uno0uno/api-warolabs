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
    Get system alerts for inventory, top products, and operational issues
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        alerts = []

        async with get_db_connection() as conn:
            # 1. Low stock alerts (critical and warning)
            low_stock_query = """
                SELECT
                    i.name,
                    i.unit,
                    ti.current_stock,
                    ti.minimum_stock,
                    (ti.minimum_stock - ti.current_stock) as deficit,
                    CASE
                        WHEN ti.current_stock <= 0 THEN 'critical'
                        WHEN ti.current_stock <= ti.minimum_stock * 0.5 THEN 'critical'
                        ELSE 'warning'
                    END as severity
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                WHERE ti.tenant_id = $1
                    AND ti.current_stock <= ti.minimum_stock
                ORDER BY
                    CASE
                        WHEN ti.current_stock <= 0 THEN 0
                        WHEN ti.current_stock <= ti.minimum_stock * 0.5 THEN 1
                        ELSE 2
                    END,
                    deficit DESC
                LIMIT 5
            """
            low_stock_rows = await conn.fetch(low_stock_query, tenant_id)

            for row in low_stock_rows:
                if row['current_stock'] <= 0:
                    title = f"Sin stock: {row['name']}"
                    description = f"El ingrediente está agotado. Stock mínimo: {row['minimum_stock']}{row['unit']}"
                else:
                    title = f"Stock bajo: {row['name']}"
                    description = f"Stock actual: {row['current_stock']}{row['unit']}. Mínimo: {row['minimum_stock']}{row['unit']}"

                alerts.append({
                    "id": f"stock_{len(alerts)}",
                    "type": row['severity'],
                    "title": title,
                    "description": description,
                    "action": {
                        "label": "Pedir ahora",
                        "url": "/abastecimiento/compras/crear"
                    }
                })

            # 2. Expiration warnings (next 7 days)
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
                LIMIT 3
            """
            expiration_rows = await conn.fetch(expiration_query, tenant_id)

            for row in expiration_rows:
                days = row['days_until_expiry']
                if days <= 0:
                    title = f"Vencido: {row['name']}"
                    severity = "critical"
                elif days <= 2:
                    title = f"Vence en {days} días: {row['name']}"
                    severity = "critical"
                else:
                    title = f"Vence en {days} días: {row['name']}"
                    severity = "warning"

                description = f"Lote {row['lote_actual']} vence el {row['fecha_vencimiento'].strftime('%d/%m/%Y')}. Stock: {row['current_stock']}"

                alerts.append({
                    "id": f"expiry_{len(alerts)}",
                    "type": severity,
                    "title": title,
                    "description": description,
                    "action": {
                        "label": "Ver detalle",
                        "url": "/inventario"
                    }
                })

            # 3. Top selling products (for reordering ingredients)
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
                    LIMIT 2
                """
                top_products_rows = await conn.fetch(top_products_query, tenant_id)

                for row in top_products_rows:
                    alerts.append({
                        "id": f"top_{len(alerts)}",
                        "type": "info",
                        "title": f"Producto popular: {row['name']}",
                        "description": f"{row['order_count']} pedidos esta semana ({row['total_units']} unidades). Revisa stock de ingredientes.",
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
