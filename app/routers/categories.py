from fastapi import APIRouter, Request
from app.database import get_db_connection
from app.models.category import CategoriesListResponse, Category

router = APIRouter()

@router.get("", response_model=CategoriesListResponse)
async def get_categories_endpoint(request: Request):
    """
    Get all categories.

    Returns list of product categories available in the system.
    """
    async with get_db_connection() as conn:
        query = """
            SELECT
                id,
                name,
                description,
                created_at,
                updated_at
            FROM categories
            ORDER BY name ASC
        """

        rows = await conn.fetch(query)
        categories = [Category(**dict(row)) for row in rows]

        return CategoriesListResponse(
            success=True,
            total=len(categories),
            data=categories
        )
