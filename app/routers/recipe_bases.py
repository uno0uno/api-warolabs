"""
Recipe Bases Router - HTTP endpoints for recipe base types management
"""
from fastapi import APIRouter, Request, Response, Query, Body, Path
from typing import Optional
from uuid import UUID
from app.services.recipe_bases_service import (
    create_recipe_base_type,
    get_recipe_base_types_list,
    get_recipe_base_type_by_id,
    update_recipe_base_type,
    delete_recipe_base_type
)
from app.models.recipe_base import (
    RecipeBaseTypeCreate,
    RecipeBaseTypeUpdate,
    RecipeBaseTypeResponse,
    RecipeBaseTypesListResponse
)

router = APIRouter()


@router.post("", response_model=RecipeBaseTypeResponse, status_code=201)
async def create_recipe_base_endpoint(
    request: Request,
    recipe_data: RecipeBaseTypeCreate = Body(...)
):
    """
    Create a new recipe base type with its ingredient templates.

    This endpoint creates a product base type (recipe template) that can be
    reused across multiple products.

    **Request Body:**
    - name: Name of the recipe base (e.g., "Pizza Italiana Clásica")
    - description: Optional description
    - is_active: Whether the recipe is active (default: true)
    - ingredients: List of ingredients with quantities

    **Example:**
    ```json
    {
        "name": "Pizza Italiana Clásica",
        "description": "Receta tradicional de pizza italiana",
        "is_active": true,
        "ingredients": [
            {
                "ingredient_id": "uuid-here",
                "base_quantity": 300,
                "unit": "g",
                "is_required": true,
                "notes": "Usar harina 00"
            }
        ]
    }
    ```

    **Returns:**
    - RecipeBaseTypeResponse with the created recipe base type
    """
    return await create_recipe_base_type(request, recipe_data)


@router.get("", response_model=RecipeBaseTypesListResponse)
async def get_recipe_bases_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by name"),
    is_active: Optional[bool] = Query(default=None, description="Filter by active status"),
    include_ingredients: bool = Query(default=False, description="Include ingredients in response")
):
    """
    Get list of recipe base types with optional filtering and pagination.

    **Query Parameters:**
    - page: Page number (default: 1)
    - limit: Items per page (default: 50, max: 250)
    - search: Search term for recipe name
    - is_active: Filter by active status (true/false)
    - include_ingredients: Include ingredient details (default: false)

    **Returns:**
    - RecipeBaseTypesListResponse with paginated list
    """
    return await get_recipe_base_types_list(
        request,
        response,
        page,
        limit,
        search,
        is_active,
        include_ingredients
    )


@router.get("/{recipe_base_id}", response_model=RecipeBaseTypeResponse)
async def get_recipe_base_by_id_endpoint(
    request: Request,
    recipe_base_id: UUID = Path(..., description="Recipe base type UUID")
):
    """
    Get a single recipe base type by ID with all its ingredients.

    **Path Parameters:**
    - recipe_base_id: UUID of the recipe base type

    **Returns:**
    - RecipeBaseTypeResponse with recipe base type and ingredients
    """
    return await get_recipe_base_type_by_id(request, recipe_base_id)


@router.put("/{recipe_base_id}", response_model=RecipeBaseTypeResponse)
async def update_recipe_base_endpoint(
    request: Request,
    recipe_base_id: UUID = Path(..., description="Recipe base type UUID"),
    update_data: RecipeBaseTypeUpdate = Body(...)
):
    """
    Update a recipe base type and optionally its ingredients.

    This endpoint can update the basic information (name, description, is_active)
    and the ingredients list. If ingredients are provided, all existing ingredients
    will be replaced with the new list.

    **Path Parameters:**
    - recipe_base_id: UUID of the recipe base type

    **Request Body:**
    - name: Optional new name
    - description: Optional new description
    - is_active: Optional active status
    - ingredients: Optional list of ingredients (replaces all existing)

    **Returns:**
    - RecipeBaseTypeResponse with updated recipe base type
    """
    return await update_recipe_base_type(request, recipe_base_id, update_data)


@router.delete("/{recipe_base_id}", status_code=200)
async def delete_recipe_base_endpoint(
    request: Request,
    recipe_base_id: UUID = Path(..., description="Recipe base type UUID")
):
    """
    Delete a recipe base type and its ingredient templates.

    **Warning:** This will permanently delete the recipe base type and all
    its ingredient templates. Products using this recipe base will not be affected.

    **Path Parameters:**
    - recipe_base_id: UUID of the recipe base type to delete

    **Returns:**
    - Success message
    """
    return await delete_recipe_base_type(request, recipe_base_id)
