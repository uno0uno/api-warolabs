from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
import logging
from app.core.exceptions import AuthenticationError
from app.models.ingredient import Ingredient, IngredientsListResponse

logger = logging.getLogger(__name__)

async def get_ingredients_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    category: Optional[str] = None,
    supplier_id: Optional[UUID] = None
) -> IngredientsListResponse:
    """
    Fetches a list of ingredients from the database with tenant isolation,
    joining with tenant_supplier_prices to get the current price.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Base query joins ingredients with the latest price from tenant_supplier_prices
            base_query = """
                SELECT
                    i.id,
                    i.tenant_id,
                    i.name,
                    i.unit,
                    i.category,
                    i.description,
                    CAST(i.minimum_order_quantity AS float) as minimum_order_quantity,
                    i.created_at,
                    i.updated_at,
                    CAST(tsp.unit_price AS float) as price,
                    tsp.supplier_id
                FROM ingredients i
                LEFT JOIN (
                    SELECT 
                        ingredient_id,
                        supplier_id,
                        unit_price,
                        ROW_NUMBER() OVER(PARTITION BY ingredient_id ORDER BY effective_date DESC, created_at DESC) as rn
                    FROM tenant_supplier_prices
                    WHERE tenant_id = $1 AND is_active = TRUE
                ) tsp ON i.id = tsp.ingredient_id AND tsp.rn = 1
                WHERE i.tenant_id = $1
            """
            
            count_query = "SELECT COUNT(*) FROM ingredients WHERE tenant_id = $1"
            
            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(i.name) LIKE LOWER(${param_count}) OR LOWER(i.description) LIKE LOWER(${param_count}))"
                count_query += f" AND (LOWER(name) LIKE LOWER(${param_count}) OR LOWER(description) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1
            
            if category:
                base_query += f" AND LOWER(i.category) = LOWER(${param_count})"
                count_query += f" AND LOWER(category) = LOWER(${param_count})"
                params.append(category)
                param_count += 1

            if supplier_id:
                base_query += f" AND tsp.supplier_id = ${param_count}"
                # Note: Filtering count by supplier_id would require a join in the count query as well.
                # For simplicity, we'll count all ingredients and filter the result set.
                param_count += 1

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY i.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            # Execute queries
            ingredients_data = await conn.fetch(base_query, *params)
            count_result = await conn.fetchrow(count_query, *params[:-2]) # Exclude limit and offset

            # Process results into Pydantic models
            ingredients = []
            from pydantic import ValidationError
            for row in ingredients_data:
                try:
                    ingredients.append(Ingredient(**row))
                except ValidationError as e:
                    # Continue to the next row instead of raising
                    continue

            return IngredientsListResponse(
                success=True,
                total=count_result['count'],
                data=ingredients
            )

    except AuthenticationError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")