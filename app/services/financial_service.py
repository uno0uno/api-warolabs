import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.middleware import require_valid_session

logger = logging.getLogger(__name__)

async def get_products_analysis(request: Request, response: Response, period: int = 365, category: Optional[str] = None, min_margin: Optional[int] = None, sort_by: str = "margin") -> Dict[str, Any]:
    """
    Products analysis - try real data first, fallback to empty response
    """
    # Get session context from middleware
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    
    if not tenant_id:
        raise ValueError("Tenant ID no puede ser null")
    
    async with get_db_connection() as conn:
        # Check if tenant has any orders
        orders_check = await conn.fetchrow("""
            SELECT COUNT(o.*) as count FROM orders o
            INNER JOIN tenant_members tm ON o.user_id = tm.user_id
            WHERE tm.tenant_id = $1 AND o.status = 'completed'
        """, tenant_id)
        
        # Use exact same logic as warolabs.com - try real data first
        try:
            # Exact SQL query from warolabs.com with parameters properly escaped
            products_query = f"""
                WITH recent_orders AS (
                  SELECT 
                    o.id as order_id,
                    o.order_date,
                    o.total_amount,
                    o.user_id,
                    oi.variant_id,
                    oi.quantity,
                    oi.price_at_purchase,
                    tm.tenant_id
                  FROM orders o
                  INNER JOIN order_items oi ON o.id = oi.order_id
                  INNER JOIN tenant_members tm ON o.user_id = tm.user_id
                  WHERE o.status = 'completed'
                    AND tm.tenant_id = $1::uuid
                    AND o.order_date >= NOW() - INTERVAL '{period} days'
                ),
                product_analytics AS (
                  SELECT 
                    p.id,
                    p.name,
                    p.price,
                    c.name as category_name,
                    -- Datos reales de ventas
                    COALESCE(SUM(ro.quantity), 0) as total_sales,
                    COALESCE(SUM(ro.price_at_purchase * ro.quantity), 0) as total_revenue,
                    -- Calcular costo estimado basado en precio (60% del precio como costo)
                    p.price * 0.6 as avg_unit_cost,
                    -- Calcular margen basado en precio menos costo estimado
                    CASE 
                      WHEN p.price > 0 THEN
                        ROUND(((p.price - (p.price * 0.6)) / p.price) * 100, 2)
                      ELSE 0
                    END as real_margin,
                    -- Calcular ganancia real basada en ventas menos costos estimados
                    COALESCE(
                      SUM(ro.quantity * (ro.price_at_purchase - (p.price * 0.6))), 
                      0
                    ) as real_profit,
                    -- Contar órdenes en el período
                    COUNT(DISTINCT ro.order_id) as order_count,
                    -- Fecha de último pedido
                    MAX(ro.order_date) as last_order_date,
                    -- TIR impact basado en contribución a ingresos totales
                    CASE 
                      WHEN SUM(ro.price_at_purchase * ro.quantity) > 0 THEN
                        ROUND(
                          (SUM(ro.price_at_purchase * ro.quantity) / 
                           NULLIF((SELECT SUM(total_amount) FROM orders o2 
                                  INNER JOIN tenant_members tm2 ON o2.user_id = tm2.user_id 
                                  WHERE o2.status = 'completed' 
                                  AND o2.order_date >= NOW() - INTERVAL '{period} days'), 0)
                          ) * 100, 2
                        )
                      ELSE 0
                    END as tir_impact_percentage
                  FROM product p
                  LEFT JOIN categories c ON p.category_id = c.id
                  LEFT JOIN product_variants pv ON p.id = pv.product_id
                  INNER JOIN recent_orders ro ON pv.id = ro.variant_id
                  GROUP BY p.id, p.name, p.price, c.name
                  HAVING SUM(ro.quantity) > 0
                ),
                categorized_products AS (
                  SELECT 
                    *,
                    -- Clasificar productos según rendimiento real
                    CASE 
                      WHEN real_margin >= 70 AND total_sales >= 20 THEN 'Star'
                      WHEN real_margin >= 60 AND total_sales >= 10 THEN 'Potential'  
                      WHEN real_margin >= 50 AND total_sales >= 5 THEN 'Average'
                      WHEN real_margin < 50 OR total_sales < 5 THEN 'Low Performance'
                      ELSE 'Problematic'
                    END as classification
                  FROM product_analytics
                )
                SELECT * FROM categorized_products
                WHERE 1=1
            """
            
            # Add filters exactly like warolabs.com
            if category:
                products_query += f" AND LOWER(category_name) = LOWER('{category}')"
            
            if min_margin:
                products_query += f" AND real_margin >= {min_margin}"
            
            # Add ordering exactly like warolabs.com
            order_mapping = {
                'margin': 'real_margin',
                'sales': 'total_sales', 
                'profit': 'real_profit',
                'impact': 'tir_impact_percentage'
            }
            order_clause = order_mapping.get(sort_by, 'real_margin')
            products_query += f" ORDER BY {order_clause} DESC LIMIT 50"
            
            products_data = await conn.fetch(products_query, tenant_id)
            
            if products_data:
                return await process_products_data(conn, products_data, tenant_id, period, category, min_margin, sort_by)
            else:
                return generate_empty_response(period, category, min_margin, sort_by)
                
        except Exception as e:
            logger.error(f"Error fetching products data: {e}")
            return generate_empty_response(period, category, min_margin, sort_by)

async def get_obstacles_analysis(request: Request, response: Response, period: int = 30) -> Dict[str, Any]:
    """
    Obstacles analysis - try real data first, fallback to empty response
    """
    # Get session context from middleware
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    
    if not tenant_id:
        raise ValueError("Tenant ID no puede ser null")
    
    
    async with get_db_connection() as conn:
        try:
            # Comprehensive obstacles analysis using real data
            obstacles_query = f"""
                WITH recent_orders AS (
                  SELECT 
                    o.id as order_id,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.user_id,
                    tm.tenant_id
                  FROM orders o
                  INNER JOIN tenant_members tm ON o.user_id = tm.user_id
                  WHERE o.order_date >= NOW() - INTERVAL '{period} days'
                    AND tm.tenant_id = $1::uuid
                ),
                payment_analysis AS (
                  SELECT 
                    COUNT(*) as total_payments,
                    COUNT(CASE WHEN p.status = 'APPROVED' THEN 1 END) as successful_payments,
                    COUNT(CASE WHEN p.status IN ('DECLINED', 'FAILED', 'PENDING') THEN 1 END) as failed_payments,
                    COALESCE(SUM(CASE WHEN p.status IN ('DECLINED', 'FAILED') THEN p.amount ELSE 0 END), 0) as lost_revenue,
                    STRING_AGG(DISTINCT p.status_message, ', ') as failure_reasons
                  FROM payments p
                  INNER JOIN orders o ON p.order_id = o.id
                  INNER JOIN tenant_members tm ON o.user_id = tm.user_id
                  WHERE p.payment_date >= NOW() - INTERVAL '{period} days'
                    AND tm.tenant_id = $1::uuid
                ),
                inventory_analysis AS (
                  SELECT 
                    COUNT(*) as total_products,
                    COUNT(CASE WHEN pv.stock_quantity = 0 THEN 1 END) as out_of_stock,
                    COUNT(CASE WHEN pv.stock_quantity > 0 AND pv.stock_quantity <= 5 THEN 1 END) as low_stock,
                    COUNT(CASE WHEN pv.stock_quantity > 5 THEN 1 END) as healthy_stock,
                    0 as inventory_issues,
                    0 as units_lost
                  FROM product_variants pv
                  INNER JOIN product p ON pv.product_id = p.id
                  WHERE pv.is_active = true
                ),
                order_processing_analysis AS (
                  SELECT 
                    COUNT(*) as total_orders,
                    COUNT(CASE WHEN ro.status = 'completed' THEN 1 END) as completed_orders,
                    COUNT(CASE WHEN ro.status = 'cancelled' THEN 1 END) as cancelled_orders,
                    COUNT(CASE WHEN ro.status IN ('pending', 'processing') THEN 1 END) as stuck_orders,
                    0 as delayed_orders,
                    STRING_AGG(DISTINCT CASE WHEN ro.status = 'cancelled' THEN 'cancelled' END, ', ') as cancellation_reasons
                  FROM recent_orders ro
                ),
                financial_metrics AS (
                  SELECT 
                    COALESCE(SUM(ro.total_amount), 0) as total_revenue,
                    COALESCE(AVG(ro.total_amount), 0) as avg_order_value,
                    50000 as total_investment
                  FROM recent_orders ro
                  WHERE ro.status = 'completed'
                )
                SELECT 
                  -- Payment metrics
                  COALESCE(pa.total_payments, 0) as total_payments,
                  COALESCE(pa.successful_payments, 0) as successful_payments,
                  COALESCE(pa.failed_payments, 0) as failed_payments,
                  COALESCE(pa.lost_revenue, 0) as lost_revenue,
                  COALESCE(pa.failure_reasons, '') as failure_reasons,
                  ROUND(
                    CASE WHEN COALESCE(pa.total_payments, 0) > 0 THEN
                      (COALESCE(pa.failed_payments, 0)::DECIMAL / pa.total_payments) * 100
                    ELSE 0 END, 2
                  ) as payment_failure_rate,
                  
                  -- Inventory metrics
                  COALESCE(ia.total_products, 0) as total_products,
                  COALESCE(ia.out_of_stock, 0) as out_of_stock,
                  COALESCE(ia.low_stock, 0) as low_stock,
                  COALESCE(ia.healthy_stock, 0) as healthy_stock,
                  COALESCE(ia.inventory_issues, 0) as inventory_issues,
                  COALESCE(ia.units_lost, 0) as units_lost,
                  ROUND(
                    CASE WHEN COALESCE(ia.total_products, 0) > 0 THEN
                      ((COALESCE(ia.out_of_stock, 0) + COALESCE(ia.low_stock, 0))::DECIMAL / ia.total_products) * 100
                    ELSE 0 END, 2
                  ) as stock_risk_percentage,
                  
                  -- Order processing metrics
                  COALESCE(opa.total_orders, 0) as total_orders,
                  COALESCE(opa.completed_orders, 0) as completed_orders,
                  COALESCE(opa.cancelled_orders, 0) as cancelled_orders,
                  COALESCE(opa.stuck_orders, 0) as stuck_orders,
                  COALESCE(opa.delayed_orders, 0) as delayed_orders,
                  COALESCE(opa.cancellation_reasons, '') as cancellation_reasons,
                  ROUND(
                    CASE WHEN COALESCE(opa.total_orders, 0) > 0 THEN
                      (COALESCE(opa.cancelled_orders, 0)::DECIMAL / opa.total_orders) * 100
                    ELSE 0 END, 2
                  ) as cancellation_rate,
                  ROUND(
                    CASE WHEN COALESCE(opa.total_orders, 0) > 0 THEN
                      (COALESCE(opa.completed_orders, 0)::DECIMAL / opa.total_orders) * 100
                    ELSE 0 END, 2
                  ) as order_success_rate,
                  
                  -- Financial metrics
                  COALESCE(fm.total_revenue, 0) as total_revenue,
                  COALESCE(fm.avg_order_value, 0) as avg_order_value,
                  COALESCE(fm.total_investment, 50000) as total_investment
                  
                FROM payment_analysis pa
                FULL OUTER JOIN inventory_analysis ia ON true
                FULL OUTER JOIN order_processing_analysis opa ON true
                FULL OUTER JOIN financial_metrics fm ON true
            """
            
            obstacles_result = await conn.fetchrow(obstacles_query, tenant_id)
            
            if obstacles_result:
                return process_obstacles_data(dict(obstacles_result), period)
            else:
                return generate_empty_obstacles_response(period)
                
        except Exception as e:
            logger.error(f"Error fetching obstacles data: {e}")
            return generate_empty_obstacles_response(period)

def process_obstacles_data(data, period):
    """Process real obstacles data exactly like warolabs.com"""
    
    # Generate insights and recommendations based on real data
    insights = {
        "critical_obstacles": [],
        "warning_obstacles": [],
        "recommendations": []
    }
    
    # Critical obstacles (immediate action needed)
    if float(data.get('payment_failure_rate', 0)) > 10:
        insights["critical_obstacles"].append({
            "type": "payment_processing",
            "severity": "critical",
            "title": "High Payment Failure Rate",
            "description": f"{data['payment_failure_rate']}% of payments are failing",
            "impact": f"Lost revenue: ${round(float(data.get('lost_revenue', 0)))}",
            "action": "Check payment gateway configuration and customer payment methods",
            "details": data.get('failure_reasons', 'Various payment issues detected')
        })
    
    if float(data.get('stock_risk_percentage', 0)) > 30:
        insights["critical_obstacles"].append({
            "type": "inventory_management",
            "severity": "critical",
            "title": "Inventory Crisis",
            "description": f"{data['stock_risk_percentage']}% of products have stock issues",
            "impact": f"{data['out_of_stock']} products out of stock, {data['low_stock']} running low",
            "action": "Immediate inventory replenishment and supply chain review",
            "details": f"{data['units_lost']} units lost due to damage/expiration"
        })
    
    if float(data.get('cancellation_rate', 0)) > 15:
        insights["critical_obstacles"].append({
            "type": "order_fulfillment",
            "severity": "critical",
            "title": "High Order Cancellation Rate",
            "description": f"{data['cancellation_rate']}% of orders are being cancelled",
            "impact": f"{data['cancelled_orders']} cancelled orders out of {data['total_orders']} total",
            "action": "Investigate cancellation reasons and improve order process",
            "details": data.get('cancellation_reasons', 'Various cancellation reasons')
        })
    
    # Warning obstacles (attention needed)
    if int(data.get('stuck_orders', 0)) > 0:
        insights["warning_obstacles"].append({
            "type": "operational_efficiency",
            "severity": "warning",
            "title": "Orders Stuck in Processing",
            "description": f"{data['stuck_orders']} orders stuck in pending/processing state",
            "impact": "Customer satisfaction and cash flow affected",
            "action": "Review order workflow and resolve processing bottlenecks"
        })
    
    if int(data.get('delayed_orders', 0)) > 0:
        insights["warning_obstacles"].append({
            "type": "logistics",
            "severity": "warning",
            "title": "Order Processing Delays",
            "description": f"{data['delayed_orders']} orders experienced processing delays",
            "impact": "Customer satisfaction at risk",
            "action": "Optimize fulfillment process and improve logistics"
        })
    
    if int(data.get('inventory_issues', 0)) > 0:
        insights["warning_obstacles"].append({
            "type": "inventory_quality",
            "severity": "warning",
            "title": "Inventory Quality Issues",
            "description": f"{data['inventory_issues']} inventory adjustments due to damage/loss",
            "impact": f"{data['units_lost']} units lost this period",
            "action": "Review storage conditions and supplier quality standards"
        })
    
    # Generate recommendations
    if insights["critical_obstacles"]:
        insights["recommendations"].extend([
            "Address critical obstacles immediately - they directly impact revenue",
            "Set up monitoring alerts for payment processing and inventory levels"
        ])
    
    if insights["warning_obstacles"]:
        insights["recommendations"].append("Review operational processes to prevent warning issues from becoming critical")
    
    if not insights["critical_obstacles"] and not insights["warning_obstacles"]:
        insights["recommendations"].extend([
            "Operations running smoothly - focus on growth and optimization opportunities",
            "Consider implementing preventive monitoring systems"
        ])
    
    # Calculate health score
    health_score = max(0, 100 - (len(insights["critical_obstacles"]) * 25) - (len(insights["warning_obstacles"]) * 10))
    
    return {
        "metrics": {
            # Payment Health
            "payment_failure_rate": float(data.get('payment_failure_rate', 0)),
            "failed_payments_count": int(data.get('failed_payments', 0)),
            "successful_payments_count": int(data.get('successful_payments', 0)),
            "lost_revenue_payments": round(float(data.get('lost_revenue', 0))),
            
            # Inventory Health
            "stock_risk_percentage": float(data.get('stock_risk_percentage', 0)),
            "out_of_stock_count": int(data.get('out_of_stock', 0)),
            "low_stock_count": int(data.get('low_stock', 0)),
            "healthy_stock_count": int(data.get('healthy_stock', 0)),
            "inventory_issues_count": int(data.get('inventory_issues', 0)),
            "units_lost": int(data.get('units_lost', 0)),
            
            # Order Processing Health
            "order_success_rate": float(data.get('order_success_rate', 0)),
            "cancellation_rate": float(data.get('cancellation_rate', 0)),
            "completed_orders_count": int(data.get('completed_orders', 0)),
            "cancelled_orders_count": int(data.get('cancelled_orders', 0)),
            "stuck_orders_count": int(data.get('stuck_orders', 0)),
            "delayed_orders_count": int(data.get('delayed_orders', 0)),
            
            # Financial Health
            "total_orders": int(data.get('total_orders', 0)),
            "total_revenue": round(float(data.get('total_revenue', 0))),
            "avg_order_value": round(float(data.get('avg_order_value', 0))),
            "total_investment": round(float(data.get('total_investment', 50000)))
        },
        "obstacles_summary": {
            "critical_count": len(insights["critical_obstacles"]),
            "warning_count": len(insights["warning_obstacles"]),
            "total_obstacles": len(insights["critical_obstacles"]) + len(insights["warning_obstacles"]),
            "health_score": round(health_score)
        },
        "insights": insights,
        "period_info": {
            "period_days": period,
            "analysis_date": datetime.now().isoformat()
        }
    }

def generate_empty_obstacles_response(period):
    """Generate empty obstacles response when no data is available"""
    return {
        "metrics": {
            "payment_failure_rate": 0,
            "failed_payments_count": 0,
            "successful_payments_count": 0,
            "lost_revenue_payments": 0,
            "stock_risk_percentage": 0,
            "out_of_stock_count": 0,
            "low_stock_count": 0,
            "healthy_stock_count": 0,
            "inventory_issues_count": 0,
            "units_lost": 0,
            "order_success_rate": 0,
            "cancellation_rate": 0,
            "completed_orders_count": 0,
            "cancelled_orders_count": 0,
            "stuck_orders_count": 0,
            "delayed_orders_count": 0,
            "total_orders": 0,
            "total_revenue": 0,
            "avg_order_value": 0,
            "total_investment": 50000
        },
        "obstacles_summary": {
            "critical_count": 0,
            "warning_count": 0,
            "total_obstacles": 0,
            "health_score": 100
        },
        "insights": {
            "critical_obstacles": [],
            "warning_obstacles": [],
            "recommendations": ["No data available for analysis - ensure orders and payments are being processed"]
        },
        "period_info": {
            "period_days": period,
            "analysis_date": datetime.now().isoformat()
        }
    }

async def process_products_data(conn, products_data, tenant_id, period, category, min_margin, sort_by):
    """Process real products data exactly like warolabs.com"""
    analytics = [dict(row) for row in products_data]
    
    # Calculate metrics
    best_margin_product = max(analytics, key=lambda x: x.get('real_margin', 0), default=None)
    active_products = len([p for p in analytics if p.get('total_sales', 0) > 0])
    low_performance_products = len([p for p in analytics if p.get('classification') in ['Low Performance', 'Problematic']])
    
    total_revenue = sum(float(p.get('total_revenue', 0)) for p in analytics)
    total_profit = sum(float(p.get('real_profit', 0)) for p in analytics)
    
    # Get categories exactly like warolabs.com
    categories_query = f"""
        WITH recent_orders_for_categories AS (
          SELECT DISTINCT oi.variant_id
          FROM orders o
          INNER JOIN order_items oi ON o.id = oi.order_id
          INNER JOIN tenant_members tm ON o.user_id = tm.user_id
          WHERE o.status = 'completed'
            AND tm.tenant_id = $1::uuid
            AND o.order_date >= NOW() - INTERVAL '{period} days'
        )
        SELECT DISTINCT c.name as category_name
        FROM categories c
        INNER JOIN product p ON c.id = p.category_id
        INNER JOIN product_variants pv ON p.id = pv.product_id
        INNER JOIN recent_orders_for_categories ro ON pv.id = ro.variant_id
        WHERE c.name IS NOT NULL AND c.name != ''
        ORDER BY c.name
    """
    
    categories_data = await conn.fetch(categories_query, tenant_id)
    categories = [row['category_name'] for row in categories_data]
    
    # Generate insights exactly like warolabs.com
    star_products = [p for p in analytics if p.get('classification') == 'Star']
    problematic_products = [p for p in analytics if p.get('classification') == 'Problematic']
    
    insights = {
        "star_products": {
            "count": len(star_products),
            "revenue_percentage": round((sum(float(p.get('total_revenue', 0)) for p in star_products) / total_revenue) * 100) if total_revenue > 0 else 0,
            "top_product": star_products[0].get('name', 'N/A') if star_products else 'N/A'
        },
        "optimization_needed": {
            "count": len([p for p in analytics if float(p.get('real_margin', 0)) < 60]),
            "lowest_margin_product": min(analytics, key=lambda x: x.get('real_margin', 100), default=None)
        },
        "low_performance": {
            "count": low_performance_products,
            "potential_tir_improvement": round(low_performance_products * 0.2, 2)
        }
    }
    
    return {
        "categories": categories,
        "metrics": {
            "best_margin": {
                "percentage": float(best_margin_product.get('real_margin', 0)) if best_margin_product else 0,
                "product_name": best_margin_product.get('name', 'N/A') if best_margin_product else 'N/A'
            },
            "active_products": active_products,
            "low_performance_count": low_performance_products,
            "total_revenue": round(total_revenue),
            "total_profit": round(total_profit)
        },
        "products": [
            {
                "id": p.get('id'),
                "name": p.get('name'),
                "category": p.get('category_name', 'No category'),
                "margin": float(p.get('real_margin', 0)),
                "sales": int(p.get('total_sales', 0)),
                "cost": round(float(p.get('avg_unit_cost', 0))),
                "profit": round(float(p.get('real_profit', 0))),
                "tirImpact": float(p.get('tir_impact_percentage', 0)),
                "classification": p.get('classification'),
                "price": float(p.get('price', 0)),
                "order_count": int(p.get('order_count', 0)),
                "last_order_date": p.get('last_order_date')
            }
            for p in analytics
        ],
        "insights": insights,
        "filters": {
            "period": period,
            "category": category,
            "min_margin": min_margin,
            "sort_by": sort_by
        }
    }

def generate_empty_response(period, category, min_margin, sort_by):
    """Generate empty response when no data is available"""
    return {
        "categories": [],
        "metrics": {
            "best_margin": {
                "percentage": 0,
                "product_name": "N/A"
            },
            "active_products": 0,
            "low_performance_count": 0,
            "total_revenue": 0,
            "total_profit": 0
        },
        "products": [],
        "insights": {
            "star_products": {
                "count": 0,
                "revenue_percentage": 0,
                "top_product": "N/A"
            },
            "optimization_needed": {
                "count": 0,
                "lowest_margin_product": None
            },
            "low_performance": {
                "count": 0,
                "potential_tir_improvement": 0
            }
        },
        "filters": {
            "period": period,
            "category": category,
            "min_margin": min_margin,
            "sort_by": sort_by
        }
    }

async def get_tir_metrics(request: Request, response: Response, period: str = "monthly", limit: int = 12) -> Dict[str, Any]:
    """
    TIR metrics - try real data first, fallback to mock data
    """
    # Get session context from middleware
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    
    if not tenant_id:
        raise ValueError("Tenant ID no puede ser null")
    
    
    async with get_db_connection() as conn:
        # Check if tenant has any orders
        orders_check = await conn.fetchrow("""
            SELECT COUNT(o.*) as count FROM orders o
            INNER JOIN tenant_members tm ON o.user_id = tm.user_id
            WHERE tm.tenant_id = $1 AND o.status = 'completed'
        """, tenant_id)
        
        
        # Try to get real data first
        if orders_check['count'] > 0:
            try:
                # Calculate TIR with real cost structure for better accuracy
                real_data = await conn.fetch("""
                    WITH investment_base AS (
                        SELECT 
                            initial_investment,
                            investment_date,
                            target_tir_percentage
                        FROM tenant_investments 
                        WHERE tenant_id = $2 AND status = 'active'
                        LIMIT 1
                    ),
                    latest_costs AS (
                        SELECT 
                            -- Usar los costos más recientes disponibles
                            -- Rent: Arriendo/Alquiler
                            COALESCE(SUM(CASE WHEN ec.category_code = 'RENT' THEN te.amount ELSE 0 END), 0) as rent_costs,
                            -- Payroll: Nómina y Salarios desde tenant_expenses
                            COALESCE(SUM(CASE WHEN ec.category_code = 'PAYROLL' THEN te.amount ELSE 0 END), 0) as payroll_costs,
                            -- Utilities: Servicios Públicos
                            COALESCE(SUM(CASE WHEN ec.category_code = 'UTILITIES' THEN te.amount ELSE 0 END), 0) as utilities_costs,
                            -- Marketing: Marketing y Publicidad
                            COALESCE(SUM(CASE WHEN ec.category_code = 'MARKETING' THEN te.amount ELSE 0 END), 0) as marketing_costs,
                            -- Professional: Servicios Profesionales
                            COALESCE(SUM(CASE WHEN ec.category_code = 'PROFESSIONAL' THEN te.amount ELSE 0 END), 0) as office_costs,
                            -- Insurance: Seguros
                            COALESCE(SUM(CASE WHEN ec.category_code = 'INSURANCE' THEN te.amount ELSE 0 END), 0) as professional_costs,
                            -- Maintenance: Mantenimiento
                            COALESCE(SUM(CASE WHEN ec.category_code = 'MAINTENANCE' THEN te.amount ELSE 0 END), 0) as insurance_costs,
                            -- Supplies: Suministros Operativos
                            COALESCE(SUM(CASE WHEN ec.category_code = 'SUPPLIES' THEN te.amount ELSE 0 END), 0) as maintenance_costs,
                            -- Capital: Capital de Trabajo
                            COALESCE(SUM(CASE WHEN ec.category_code = 'CAPITAL' THEN te.amount ELSE 0 END), 0) as travel_costs,
                            -- Contingency: Contingencias
                            COALESCE(SUM(CASE WHEN ec.category_code = 'CONTINGENCY' THEN te.amount ELSE 0 END), 0) as technology_costs,
                            -- Total de todos los costos operacionales reales
                            COALESCE(SUM(te.amount), 0) as total_operational_costs
                        FROM tenant_expenses te
                        JOIN expense_categories ec ON te.expense_category_id = ec.id
                        WHERE te.tenant_id = $2
                        -- Usar el período de costos más reciente disponible
                        AND te.month_year = (
                            SELECT MAX(month_year) 
                            FROM tenant_expenses 
                            WHERE tenant_id = $2
                        )
                    ),
                    real_product_costs_by_month AS (
                        SELECT 
                            DATE_TRUNC('month', sales_month) as flow_month,
                            SUM(total_cost) as real_monthly_product_costs
                        FROM v_product_analysis
                        WHERE tenant_id = $2
                        GROUP BY DATE_TRUNC('month', sales_month)
                    ),
                    cost_ratio AS (
                        SELECT 
                            AVG(total_cost / NULLIF(total_revenue, 0)) as avg_cost_ratio
                        FROM v_product_analysis
                        WHERE tenant_id = $2 AND total_revenue > 0
                    ),
                    monthly_products_sold AS (
                        SELECT 
                            DATE_TRUNC('month', o.order_date) as flow_month,
                            COALESCE(SUM(oi.quantity), 0) as products_sold_count
                        FROM orders o
                        INNER JOIN tenant_members tm ON o.user_id = tm.user_id
                        INNER JOIN order_items oi ON o.id = oi.order_id
                        WHERE tm.tenant_id = $2 
                        AND o.status = 'completed'
                        AND o.order_date >= (SELECT investment_date FROM investment_base)
                        GROUP BY DATE_TRUNC('month', o.order_date)
                    ),
                    monthly_flows AS (
                        SELECT 
                            DATE_TRUNC('month', o.order_date) as flow_month,
                            SUM(o.total_amount) as monthly_revenue,
                            -- Usar costos reales cuando estén disponibles, sino el ratio calculado
                            COALESCE(
                                rpc.real_monthly_product_costs,
                                SUM(o.total_amount) * COALESCE(cr.avg_cost_ratio, 0.40)
                            ) as monthly_product_costs,
                            COUNT(DISTINCT o.id) as order_count,
                            COALESCE(mps.products_sold_count, 0) as products_sold_count,
                            ROW_NUMBER() OVER (ORDER BY DATE_TRUNC('month', o.order_date)) as period_number
                        FROM orders o
                        INNER JOIN tenant_members tm ON o.user_id = tm.user_id
                        LEFT JOIN monthly_products_sold mps ON mps.flow_month = DATE_TRUNC('month', o.order_date)
                        LEFT JOIN real_product_costs_by_month rpc ON rpc.flow_month = DATE_TRUNC('month', o.order_date)
                        CROSS JOIN cost_ratio cr
                        WHERE tm.tenant_id = $2 
                        AND o.status = 'completed'
                        AND o.order_date >= (SELECT investment_date FROM investment_base)
                        GROUP BY DATE_TRUNC('month', o.order_date), mps.products_sold_count, rpc.real_monthly_product_costs, cr.avg_cost_ratio
                        ORDER BY DATE_TRUNC('month', o.order_date)
                    ),
                    combined_flows AS (
                        SELECT 
                            ib.initial_investment,
                            ib.target_tir_percentage,
                            mf.flow_month as period_date,
                            mf.period_number,
                            mf.monthly_revenue as total_revenue,
                            mf.monthly_product_costs,
                            mf.order_count,
                            mf.products_sold_count,
                            COALESCE(lc.total_operational_costs, 0) as operational_costs,
                            COALESCE(lc.rent_costs, 0) as rent_costs,
                            COALESCE(lc.payroll_costs, 0) as payroll_costs,
                            COALESCE(lc.utilities_costs, 0) as utilities_costs,
                            COALESCE(lc.marketing_costs, 0) as marketing_costs,
                            COALESCE(lc.office_costs, 0) as office_costs,
                            COALESCE(lc.professional_costs, 0) as professional_costs,
                            COALESCE(lc.insurance_costs, 0) as insurance_costs,
                            COALESCE(lc.maintenance_costs, 0) as maintenance_costs,
                            COALESCE(lc.travel_costs, 0) as travel_costs,
                            COALESCE(lc.technology_costs, 0) as technology_costs,
                            -- Current Profit = Revenue - Product Costs - Operational Costs
                            mf.monthly_revenue - mf.monthly_product_costs - COALESCE(lc.total_operational_costs, 0) as current_profit,
                            -- Net Cash Flow for TIR calculation
                            CASE 
                                WHEN mf.period_number = 1 THEN 
                                    (mf.monthly_revenue - mf.monthly_product_costs - COALESCE(lc.total_operational_costs, 0)) - ib.initial_investment
                                ELSE 
                                    mf.monthly_revenue - mf.monthly_product_costs - COALESCE(lc.total_operational_costs, 0)
                            END as net_cash_flow
                        FROM investment_base ib
                        CROSS JOIN monthly_flows mf
                        CROSS JOIN latest_costs lc
                    ),
                    cash_flow_analysis AS (
                        SELECT 
                            *,
                            SUM(net_cash_flow) OVER (ORDER BY period_number) as cumulative_cash_flow,
                            COUNT(*) OVER () as total_periods
                        FROM combined_flows
                    ),
                    tir_calculation AS (
                        SELECT 
                            period_date,
                            total_revenue,
                            monthly_product_costs,
                            products_sold_count,
                            operational_costs,
                            current_profit,
                            rent_costs,
                            payroll_costs,
                            utilities_costs,
                            marketing_costs,
                            office_costs,
                            professional_costs,
                            insurance_costs,
                            maintenance_costs,
                            travel_costs,
                            technology_costs,
                            initial_investment,
                            target_tir_percentage,
                            -- TIR Real acumulado hasta el mes actual (TIR desde inicio hasta este mes)
                            CASE 
                                WHEN cumulative_cash_flow > 0 AND period_number > 0 THEN
                                    (POWER(
                                        (cumulative_cash_flow + initial_investment) / initial_investment,
                                        12.0 / period_number
                                    ) - 1) * 100
                                WHEN cumulative_cash_flow < 0 AND period_number > 0 THEN
                                    -- TIR negativo cuando aún no se recupera la inversión
                                    -1 * (POWER(
                                        ABS(cumulative_cash_flow) / initial_investment,
                                        12.0 / period_number
                                    )) * 100
                                ELSE 0
                            END as tir_actual,
                            -- TIR Proyectada (15% mayor considerando optimización de costos)
                            CASE 
                                WHEN cumulative_cash_flow > 0 AND period_number > 0 THEN
                                    ((POWER(
                                        (cumulative_cash_flow + initial_investment) / initial_investment,
                                        12.0 / period_number
                                    ) - 1) * 100) * 1.15
                                WHEN cumulative_cash_flow < 0 AND period_number > 0 THEN
                                    -- TIR proyectada negativa pero mejor que actual
                                    -1 * (POWER(
                                        ABS(cumulative_cash_flow) / initial_investment,
                                        12.0 / period_number
                                    )) * 100 * 0.85  -- 15% mejor (menos negativa)
                                ELSE 0
                            END as tir_projected,
                            -- Cálculo de recovery months más preciso
                            CASE 
                                WHEN current_profit > 0 THEN
                                    initial_investment / current_profit
                                ELSE 24
                            END as recovery_months_estimated
                        FROM cash_flow_analysis
                    )
                    SELECT 
                        period_date,
                        $1 as period_type,
                        total_revenue,
                        monthly_product_costs,
                        products_sold_count,
                        operational_costs,
                        current_profit,
                        rent_costs,
                        payroll_costs,
                        utilities_costs,
                        marketing_costs,
                        office_costs,
                        professional_costs,
                        insurance_costs,
                        maintenance_costs,
                        travel_costs,
                        technology_costs,
                        ROUND(tir_actual, 2) as tir_actual,
                        ROUND(tir_projected, 2) as tir_projected,
                        target_tir_percentage as tir_target,
                        initial_investment,
                        ROUND(recovery_months_estimated, 2) as recovery_months_estimated,
                        NOW() as calculated_at
                    FROM tir_calculation
                    ORDER BY period_date DESC 
                    LIMIT $3
                """, period, tenant_id, limit)
                
                if real_data:
                    historical_data = [dict(row) for row in real_data]
                else:
                    historical_data = None
            except Exception as e:
                logger.warning(f"Error fetching real data: {e}, falling back to mock data")
                historical_data = None
        else:
            historical_data = None
        
        # If no real data available, return empty structure
        if not historical_data:
            historical_data = []
        
        # Process results with real cost structure data
        total_revenue_12_months = sum(float(row.get('total_revenue', 0)) for row in historical_data)
        total_costs_12_months = sum(float(row.get('operational_costs', 0)) for row in historical_data)
        total_product_costs_12_months = sum(float(row.get('monthly_product_costs', 0)) for row in historical_data)
        total_products_sold_12_months = sum(int(row.get('products_sold_count', 0)) for row in historical_data)
        total_profit_12_months = sum(float(row.get('current_profit', 0)) for row in historical_data)
        
        # Calculate metrics from real data
        total_months = len(historical_data)
        
        if total_months > 0:
            # Calculate from real data with actual costs and profits
            avg_tir_actual = sum(float(row.get('tir_actual', 0)) for row in historical_data) / total_months
            avg_tir_projected = sum(float(row.get('tir_projected', 0)) for row in historical_data) / total_months
            avg_tir_target = float(historical_data[0].get('tir_target', 15.0)) if historical_data else 15.0
            total_investment = float(historical_data[0].get('initial_investment', 25000000)) if historical_data else 25000000
            avg_monthly_profit = total_profit_12_months / total_months  # Use actual profit for recovery calculation
            recovery_months = total_investment / avg_monthly_profit if avg_monthly_profit > 0 else 0
        else:
            # If no data, use zeros
            total_investment = 0
            avg_tir_target = 0
            avg_tir_actual = 0
            avg_tir_projected = 0
            recovery_months = 0
        
        current_metrics = {
            "tir_actual": round(avg_tir_actual, 2),
            "tir_projected": round(avg_tir_projected, 2),
            "tir_target": round(avg_tir_target, 2),
            "recovery_months": round(recovery_months, 2),
            "total_revenue": round(total_revenue_12_months, 2),
            "current_profit": round(total_profit_12_months, 2),
            "operational_costs": round(total_costs_12_months, 2)
        }
        
        # Datos para gráficos
        chart_data = {
            "labels": [
                row['period_date'].strftime('%b %y') if hasattr(row['period_date'], 'strftime') 
                else datetime.strptime(str(row['period_date']).split(' ')[0], '%Y-%m-%d').strftime('%b %y')
                for row in historical_data
            ],
            "actual_tir": [round(float(row.get('tir_actual', 0)), 2) for row in historical_data],
            "projected_tir": [round(float(row.get('tir_projected', 0)), 2) for row in historical_data],
            "target_tir": [round(float(current_metrics['tir_target']), 2) for _ in historical_data]
        }
        
        # Datos tabulares con costos reales y profit actual
        table_data = {
            "actual": [
                {
                    "month": row['period_date'].strftime('%B') if hasattr(row['period_date'], 'strftime') 
                        else datetime.strptime(str(row['period_date']).split(' ')[0], '%Y-%m-%d').strftime('%B'),
                    "tir": round(float(row.get('tir_actual', 0)), 2),
                    "investment": round(float(row.get('initial_investment', 50000)), 2),
                    "monthlyRevenue": round(float(row.get('total_revenue', 0)), 2),
                    "monthly_product_costs": round(float(row.get('monthly_product_costs', 0)), 2),
                    "products_sold_count": int(row.get('products_sold_count', 0)),
                    "costs": round(float(row.get('operational_costs', 0)), 2),
                    "profit": round(float(row.get('current_profit', 0)), 2)
                }
                for row in historical_data[:limit]
            ],
            "projected": [
                {
                    "month": row['period_date'].strftime('%B') if hasattr(row['period_date'], 'strftime') 
                        else datetime.strptime(str(row['period_date']).split(' ')[0], '%Y-%m-%d').strftime('%B'),
                    "tir": round(float(row.get('tir_projected', 0)), 2),
                    "investment": round(float(row.get('initial_investment', 50000)), 2),
                    "monthlyRevenue": round(float(row.get('total_revenue', 0)) * 1.1, 2),
                    "costs": round(float(row.get('operational_costs', 0)) * 0.9, 2),  # Optimización de costos 10%
                    "profit": round(float(row.get('current_profit', 0)) * 1.25, 2)  # Mejora esperada 25%
                }
                for row in historical_data[:limit]
            ],
            "totals": {
                "actual": {
                    "tir_average": round(avg_tir_actual, 2),
                    "total_investment": round(total_investment, 2),
                    "total_revenue": round(total_revenue_12_months, 2),
                    "total_product_costs": round(total_product_costs_12_months, 2),
                    "total_products_sold": total_products_sold_12_months,
                    "total_profit": round(total_profit_12_months, 2),
                    "total_costs": round(total_costs_12_months, 2),
                    "months_count": len(historical_data)
                },
                "projected": {
                    "tir_average": round(avg_tir_projected, 2),
                    "total_investment": round(total_investment, 2),
                    "total_revenue": round(total_revenue_12_months * 1.1, 2),
                    "total_profit": round(total_profit_12_months * 1.25, 2),
                    "total_costs": round(total_costs_12_months * 0.9, 2),
                    "months_count": len(historical_data)
                }
            }
        }
        
        return {
            "current": {
                "tir_actual": float(current_metrics["tir_actual"]),
                "tir_projected": float(current_metrics["tir_projected"]),
                "tir_target": float(current_metrics["tir_target"]),
                "recovery_months": float(current_metrics["recovery_months"]),
                "total_revenue": float(current_metrics["total_revenue"]),
                "current_profit": float(current_metrics["current_profit"]),
                "operational_costs": float(current_metrics["operational_costs"])
            },
            "charts": chart_data,
            "tables": table_data,
            "historical": historical_data
        }