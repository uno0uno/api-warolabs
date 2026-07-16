from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.modifier import (
    ModifierGroup, ModifierGroupCreate, ModifierGroupUpdate,
    ModifierGroupsListResponse, ModifierGroupResponse, ModifierGroupStats,
    Modifier, ProductInfo, IngredientInfo, RecipeBaseInfo,
    ModifierRecipeLine, ModifierRecipeLineBase,
)
from app.services import menu_history_service
from app.services.ingredient_purchase_units_service import resolve_to_base_unit
from app.services.modifier_option_service import (
    calculated_modifier_option_unit_cost,
    validate_modifier_option_fields,
)
import logging

logger = logging.getLogger(__name__)

_MODIFIER_SELECT_COLS = """
    m.id,
    m.modifier_group_id,
    m.name,
    m.price,
    m.max_limit,
    m.included_quantity,
    m.is_default,
    m.is_available,
    m.sort_order,
    m.created_at,
    m.updated_at,
    m.option_type,
    m.ingredient_id,
    m.ingredient_quantity,
    m.ingredient_unit,
    m.recipe_base_type_id,
    m.recipe_base_quantity,
    m.linked_product_id,
    m.linked_product_quantity,
    i.name as ingredient_name,
    i.unit as ingredient_base_unit,
    i.costo_unitario,
    i.controla_inventario,
    pbt.name as recipe_base_name,
    lp.name as linked_product_name
"""


async def _replace_modifier_recipes(
    conn,
    modifier_id: UUID,
    recipe_lines: Optional[List[ModifierRecipeLineBase]],
) -> None:
    await conn.execute("DELETE FROM modifier_recipes WHERE modifier_id = $1", modifier_id)
    if not recipe_lines:
        return
    for line in recipe_lines:
        qty, unit = await resolve_to_base_unit(
            conn,
            line.ingredient_id,
            float(line.quantity),
            line.unit,
        )
        await conn.execute(
            """
            INSERT INTO modifier_recipes (modifier_id, ingredient_id, quantity, unit)
            VALUES ($1, $2, $3, $4)
            """,
            modifier_id,
            line.ingredient_id,
            qty,
            unit,
        )


async def _fetch_modifier_recipe_lines(conn, modifier_id: UUID) -> List[ModifierRecipeLine]:
    rows = await conn.fetch(
        """
        SELECT mr.id, mr.ingredient_id, mr.quantity, mr.unit,
               i.name as ingredient_name, i.unit as ingredient_base_unit,
               i.costo_unitario, i.controla_inventario
        FROM modifier_recipes mr
        JOIN ingredients i ON mr.ingredient_id = i.id
        WHERE mr.modifier_id = $1
        ORDER BY mr.created_at
        """,
        modifier_id,
    )
    lines = []
    for r in rows:
        ing = IngredientInfo(
            id=r["ingredient_id"],
            name=r["ingredient_name"],
            unit=r["ingredient_base_unit"],
            costo_unitario=r["costo_unitario"],
            controla_inventario=r["controla_inventario"] or False,
        )
        lines.append(
            ModifierRecipeLine(
                id=r["id"],
                ingredient_id=r["ingredient_id"],
                quantity=r["quantity"],
                unit=r["unit"],
                ingredient=ing,
            )
        )
    return lines


async def _build_modifier(
    conn,
    row,
    tenant_id: UUID,
) -> Modifier:
    recipe_lines = await _fetch_modifier_recipe_lines(conn, row["id"])
    mod_dict = {
        "id": row["id"],
        "modifier_group_id": row["modifier_group_id"],
        "name": row["name"],
        "price": row["price"],
        "max_limit": row["max_limit"],
        "included_quantity": row["included_quantity"],
        "is_default": row["is_default"],
        "is_available": row["is_available"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "option_type": row["option_type"] or "INGREDIENT",
        "ingredient_id": row["ingredient_id"],
        "ingredient_quantity": row["ingredient_quantity"],
        "ingredient_unit": row["ingredient_unit"],
        "recipe_base_type_id": row["recipe_base_type_id"],
        "recipe_base_quantity": row["recipe_base_quantity"] or 1,
        "linked_product_id": row["linked_product_id"],
        "linked_product_quantity": row["linked_product_quantity"] or 1,
        "recipe_lines": recipe_lines or None,
    }
    if row["ingredient_id"]:
        mod_dict["ingredient"] = IngredientInfo(
            id=row["ingredient_id"],
            name=row["ingredient_name"],
            unit=row["ingredient_base_unit"],
            costo_unitario=row["costo_unitario"],
            controla_inventario=row["controla_inventario"] or False,
        )
    if row["recipe_base_type_id"] and row.get("recipe_base_name"):
        mod_dict["recipe_base"] = RecipeBaseInfo(
            id=row["recipe_base_type_id"],
            name=row["recipe_base_name"],
        )
    if row["linked_product_id"] and row.get("linked_product_name"):
        mod_dict["linked_product"] = ProductInfo(
            id=row["linked_product_id"],
            name=row["linked_product_name"],
        )
    mod_dict["unit_cost"] = await calculated_modifier_option_unit_cost(
        conn, row["id"], tenant_id
    )
    return Modifier(**mod_dict)


async def _insert_modifier(conn, group_id: UUID, modifier) -> UUID:
    validate_modifier_option_fields(modifier)
    option_type = (modifier.option_type or "INGREDIENT").upper()

    ing_qty = modifier.ingredient_quantity
    ing_unit = modifier.ingredient_unit
    if modifier.ingredient_id and ing_qty is not None and ing_unit:
        ing_qty, ing_unit = await resolve_to_base_unit(
            conn,
            modifier.ingredient_id,
            float(ing_qty),
            ing_unit,
        )

    row = await conn.fetchrow(
        """
        INSERT INTO modifiers (
            modifier_group_id, name, price, max_limit, included_quantity,
            is_default, is_available, sort_order,
            option_type,
            ingredient_id, ingredient_quantity, ingredient_unit,
            recipe_base_type_id, recipe_base_quantity,
            linked_product_id, linked_product_quantity
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
        RETURNING id
        """,
        group_id,
        modifier.name,
        modifier.price,
        modifier.max_limit,
        modifier.included_quantity,
        modifier.is_default,
        modifier.is_available,
        modifier.sort_order,
        option_type,
        modifier.ingredient_id if option_type == "INGREDIENT" else None,
        ing_qty if option_type == "INGREDIENT" else None,
        ing_unit if option_type == "INGREDIENT" else None,
        modifier.recipe_base_type_id if option_type == "RECIPE" else None,
        modifier.recipe_base_quantity if option_type == "RECIPE" else 1,
        modifier.linked_product_id if option_type == "PRODUCT" else None,
        modifier.linked_product_quantity if option_type == "PRODUCT" else 1,
    )
    modifier_id = row["id"]
    if option_type == "RECIPE" and modifier.recipe_lines:
        await _replace_modifier_recipes(conn, modifier_id, modifier.recipe_lines)
    return modifier_id

async def create_modifier_group(
    request: Request,
    group_data: ModifierGroupCreate
) -> ModifierGroupResponse:
    """
    Creates a modifier group with its modifiers and product associations in a single transaction.
    Now supports associating with multiple products via junction table.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or group_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Insert modifier group (without product_id)
                group_query = """
                    INSERT INTO modifier_groups (
                        tenant_id, name, min_qty, max_qty,
                        is_required, sort_order
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, created_at, updated_at
                """
                group_result = await conn.fetchrow(
                    group_query,
                    tenant_id,
                    group_data.name,
                    group_data.min_qty,
                    group_data.max_qty,
                    group_data.is_required,
                    group_data.sort_order
                )

                group_id = group_result['id']

                # 2. Insert product associations in junction table
                product_assoc_query = """
                    INSERT INTO product_modifier_groups (
                        product_id, modifier_group_id, tenant_id
                    )
                    VALUES ($1, $2, $3)
                """
                for product_id in group_data.product_ids:
                    await conn.execute(
                        product_assoc_query,
                        product_id,
                        group_id,
                        tenant_id
                    )

                # 3. Insert modifiers (option types + optional modifier_recipes)
                if group_data.modifiers:
                    for modifier in group_data.modifiers:
                        await _insert_modifier(conn, group_id, modifier)

                # 4. Registrar en historial
                user_id = session_context.user_id if hasattr(session_context, 'user_id') else None
                group_snapshot = await menu_history_service.get_modifier_group_snapshot(conn, group_id, tenant_id)
                if group_snapshot:
                    await menu_history_service.record_modifier_group_create(
                        conn, tenant_id, group_id, group_data.name,
                        group_snapshot, user_id
                    )

                # 5. Get complete group with modifiers and products
                return await get_modifier_group_by_id(request, group_id, conn)

    except AuthenticationError as e:
        raise e
    except ValueError as e:
        raise APIError(str(e), status_code=400)
    except Exception as e:
        logger.error(f"Error creating modifier group: {str(e)}")
        raise APIError(f"Error creating modifier group: {str(e)}", status_code=500)


async def get_modifier_group_by_id(
    request: Request,
    group_id: UUID,
    conn=None
) -> ModifierGroupResponse:
    """Get a single modifier group with its modifiers and associated products"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async def _fetch_group(connection):
            # Get group (without product_id - now using junction table)
            group_query = """
                SELECT
                    mg.id,
                    mg.tenant_id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order,
                    mg.created_at,
                    mg.updated_at
                FROM modifier_groups mg
                WHERE mg.id = $1 AND mg.tenant_id = $2
            """

            group_row = await connection.fetchrow(group_query, group_id, tenant_id)

            if not group_row:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Get associated products from junction table
            products_query = """
                SELECT p.id, p.name
                FROM product_modifier_groups pmg
                JOIN product p ON pmg.product_id = p.id
                WHERE pmg.modifier_group_id = $1
                ORDER BY p.name
            """
            products_rows = await connection.fetch(products_query, group_id)

            modifiers_query = f"""
                SELECT {_MODIFIER_SELECT_COLS}
                FROM modifiers m
                LEFT JOIN ingredients i ON m.ingredient_id = i.id
                LEFT JOIN product_base_types pbt ON m.recipe_base_type_id = pbt.id
                LEFT JOIN product lp ON m.linked_product_id = lp.id
                WHERE m.modifier_group_id = $1
                ORDER BY m.sort_order, m.name
            """

            modifier_rows = await connection.fetch(modifiers_query, group_id)

            group_dict = dict(group_row)
            group_dict['products'] = [ProductInfo(id=row['id'], name=row['name']) for row in products_rows]

            modifiers = []
            for row in modifier_rows:
                modifiers.append(await _build_modifier(connection, row, tenant_id))

            group_dict['modifiers'] = modifiers

            return ModifierGroupResponse(data=ModifierGroup(**group_dict))

        if conn:
            return await _fetch_group(conn)
        else:
            async with get_db_connection() as connection:
                return await _fetch_group(connection)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching modifier group: {str(e)}")
        raise APIError(f"Error fetching modifier group: {str(e)}", status_code=500)


async def get_modifier_groups_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    product_id: Optional[UUID] = None,
    is_required: Optional[bool] = None
) -> ModifierGroupsListResponse:
    """Get list of modifier groups with filters. Now supports multiple products per group."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Base query - now using junction table for product filtering
            base_query = """
                SELECT DISTINCT
                    mg.id,
                    mg.tenant_id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order,
                    mg.created_at,
                    mg.updated_at
                FROM modifier_groups mg
                LEFT JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                LEFT JOIN product p ON pmg.product_id = p.id
                WHERE mg.tenant_id = $1
            """

            count_query = """
                SELECT COUNT(DISTINCT mg.id)
                FROM modifier_groups mg
                LEFT JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                LEFT JOIN product p ON pmg.product_id = p.id
                WHERE mg.tenant_id = $1
            """

            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(mg.name) LIKE LOWER(${param_count}) OR LOWER(p.name) LIKE LOWER(${param_count}))"
                count_query += f" AND (LOWER(mg.name) LIKE LOWER(${param_count}) OR LOWER(p.name) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1

            if product_id:
                base_query += f" AND pmg.product_id = ${param_count}"
                count_query += f" AND pmg.product_id = ${param_count}"
                params.append(product_id)
                param_count += 1

            if is_required is not None:
                base_query += f" AND mg.is_required = ${param_count}"
                count_query += f" AND mg.is_required = ${param_count}"
                params.append(is_required)
                param_count += 1

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY mg.sort_order, mg.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"

            # Execute queries
            groups_data = await conn.fetch(base_query, *params, limit, offset)
            count_result = await conn.fetchrow(count_query, *params)

            total = count_result['count']

            # Convert to ModifierGroup models
            groups = []
            for row in groups_data:
                group_dict = dict(row)

                # Fetch associated products from junction table
                products_query = """
                    SELECT p.id, p.name
                    FROM product_modifier_groups pmg
                    JOIN product p ON pmg.product_id = p.id
                    WHERE pmg.modifier_group_id = $1
                    ORDER BY p.name
                """
                products_rows = await conn.fetch(products_query, row['id'])
                group_dict['products'] = [ProductInfo(id=r['id'], name=r['name']) for r in products_rows]

                modifiers_query = f"""
                    SELECT {_MODIFIER_SELECT_COLS}
                    FROM modifiers m
                    LEFT JOIN ingredients i ON m.ingredient_id = i.id
                    LEFT JOIN product_base_types pbt ON m.recipe_base_type_id = pbt.id
                    LEFT JOIN product lp ON m.linked_product_id = lp.id
                    WHERE m.modifier_group_id = $1
                    ORDER BY m.sort_order, m.name
                """
                modifier_rows = await conn.fetch(modifiers_query, row['id'])

                modifiers = []
                for r in modifier_rows:
                    modifiers.append(await _build_modifier(conn, r, tenant_id))

                group_dict['modifiers'] = modifiers

                groups.append(ModifierGroup(**group_dict))

            return ModifierGroupsListResponse(
                success=True,
                total=total,
                data=groups
            )

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching modifier groups: {str(e)}")
        raise APIError(f"Error fetching modifier groups: {str(e)}", status_code=500)


async def get_modifier_group_stats(request: Request) -> ModifierGroupStats:
    """Get modifier group statistics. Now counts products from junction table."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            stats_query = """
                SELECT
                    COUNT(DISTINCT mg.id) as total_groups,
                    COUNT(DISTINCT m.id) as total_modifiers,
                    COUNT(DISTINCT pmg.product_id) as products_with_modifiers
                FROM modifier_groups mg
                LEFT JOIN modifiers m ON mg.id = m.modifier_group_id
                LEFT JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                WHERE mg.tenant_id = $1
            """

            stats_row = await conn.fetchrow(stats_query, tenant_id)

            return ModifierGroupStats(**dict(stats_row))

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching modifier group stats: {str(e)}")
        raise APIError(f"Error fetching modifier group stats: {str(e)}", status_code=500)


async def update_modifier_group(
    request: Request,
    group_id: UUID,
    group_data: ModifierGroupUpdate
) -> ModifierGroupResponse:
    """Updates a modifier group with its modifiers and product associations in a single transaction."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify group exists and belongs to tenant
            verify_query = "SELECT id, name FROM modifier_groups WHERE id = $1 AND tenant_id = $2"
            group_exists = await conn.fetchrow(verify_query, group_id, tenant_id)

            if not group_exists:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Obtener snapshot ANTES de actualizar (para historial)
            old_snapshot = await menu_history_service.get_modifier_group_snapshot(conn, group_id, tenant_id)
            group_name = group_exists['name']
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

            # Start transaction
            async with conn.transaction():
                # 1. Build update query dynamically (excluding product_ids and modifiers)
                update_fields = []
                update_values = []
                param_count = 1

                for field, value in group_data.model_dump(exclude={'modifiers', 'product_ids'}, exclude_unset=True).items():
                    if value is not None:
                        update_fields.append(f"{field} = ${param_count}")
                        update_values.append(value)
                        param_count += 1

                if update_fields:
                    update_fields.append(f"updated_at = NOW()")
                    update_query = f"""
                        UPDATE modifier_groups
                        SET {', '.join(update_fields)}
                        WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                    """
                    update_values.extend([group_id, tenant_id])
                    await conn.execute(update_query, *update_values)

                # 2. Update product associations if provided
                if group_data.product_ids is not None:
                    # Delete existing associations
                    delete_assoc_query = "DELETE FROM product_modifier_groups WHERE modifier_group_id = $1"
                    await conn.execute(delete_assoc_query, group_id)

                    # Insert new associations
                    if group_data.product_ids:
                        assoc_query = """
                            INSERT INTO product_modifier_groups (product_id, modifier_group_id, tenant_id)
                            VALUES ($1, $2, $3)
                        """
                        for product_id in group_data.product_ids:
                            await conn.execute(assoc_query, product_id, group_id, tenant_id)

                # 3. Update modifiers if provided (upsert to preserve order history)
                if group_data.modifiers is not None:
                    # Get existing modifiers
                    existing_modifiers = await conn.fetch(
                        "SELECT id, name, included_quantity FROM modifiers WHERE modifier_group_id = $1",
                        group_id
                    )
                    existing_names = {row['name']: row['id'] for row in existing_modifiers}
                    existing_included = {
                        row["id"]: row["included_quantity"] for row in existing_modifiers
                    }
                    existing_ids = set(existing_names.values())
                    modifiers_to_keep = set()

                    for modifier in group_data.modifiers:
                        validate_modifier_option_fields(modifier)
                        option_type = (modifier.option_type or "INGREDIENT").upper()

                        ing_qty = modifier.ingredient_quantity
                        ing_unit = modifier.ingredient_unit
                        if modifier.ingredient_id and ing_qty is not None and ing_unit:
                            ing_qty, ing_unit = await resolve_to_base_unit(
                                conn,
                                modifier.ingredient_id,
                                float(ing_qty),
                                ing_unit,
                            )

                        if modifier.name in existing_names:
                            mod_id = existing_names[modifier.name]
                            modifiers_to_keep.add(mod_id)
                            included_quantity = (
                                modifier.included_quantity
                                if "included_quantity" in modifier.model_fields_set
                                else existing_included[mod_id]
                            )
                            if included_quantity > modifier.max_limit:
                                raise ValueError(
                                    "included_quantity must be between 0 and max_limit"
                                )
                            await conn.execute(
                                """
                                UPDATE modifiers SET
                                    price = $2, max_limit = $3, included_quantity = $4,
                                    is_default = $5, is_available = $6, sort_order = $7,
                                    option_type = $8,
                                    ingredient_id = $9, ingredient_quantity = $10, ingredient_unit = $11,
                                    recipe_base_type_id = $12, recipe_base_quantity = $13,
                                    linked_product_id = $14, linked_product_quantity = $15
                                WHERE id = $1
                                """,
                                mod_id,
                                modifier.price,
                                modifier.max_limit,
                                included_quantity,
                                modifier.is_default,
                                modifier.is_available,
                                modifier.sort_order,
                                option_type,
                                modifier.ingredient_id if option_type == "INGREDIENT" else None,
                                ing_qty if option_type == "INGREDIENT" else None,
                                ing_unit if option_type == "INGREDIENT" else None,
                                modifier.recipe_base_type_id if option_type == "RECIPE" else None,
                                modifier.recipe_base_quantity if option_type == "RECIPE" else 1,
                                modifier.linked_product_id if option_type == "PRODUCT" else None,
                                modifier.linked_product_quantity if option_type == "PRODUCT" else 1,
                            )
                            if option_type == "RECIPE":
                                await _replace_modifier_recipes(
                                    conn, mod_id, modifier.recipe_lines
                                )
                            else:
                                await conn.execute(
                                    "DELETE FROM modifier_recipes WHERE modifier_id = $1",
                                    mod_id,
                                )
                        else:
                            await _insert_modifier(conn, group_id, modifier)

                    # Soft-delete removed modifiers (preserve order history)
                    modifiers_to_disable = existing_ids - modifiers_to_keep
                    if modifiers_to_disable:
                        await conn.execute(
                            "UPDATE modifiers SET is_available = false WHERE id = ANY($1::uuid[])",
                            list(modifiers_to_disable)
                        )

                # 4. Registrar cambios en historial
                if old_snapshot:
                    new_snapshot = await menu_history_service.get_modifier_group_snapshot(conn, group_id, tenant_id)
                    if new_snapshot:
                        await menu_history_service.compare_and_record_modifier_group_changes(
                            conn, tenant_id, group_id, group_name,
                            old_snapshot, new_snapshot, user_id
                        )

                # 5. Get complete updated group
                return await get_modifier_group_by_id(request, group_id, conn)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except ValueError as e:
        raise APIError(str(e), status_code=400)
    except Exception as e:
        logger.error(f"Error updating modifier group: {str(e)}")
        raise APIError(f"Error updating modifier group: {str(e)}", status_code=500)


async def delete_modifier_group(
    request: Request,
    group_id: UUID
) -> dict:
    """Deletes a modifier group and its modifiers."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify group exists and belongs to tenant
            verify_query = "SELECT id, name FROM modifier_groups WHERE id = $1 AND tenant_id = $2"
            group_exists = await conn.fetchrow(verify_query, group_id, tenant_id)

            if not group_exists:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Obtener snapshot ANTES de eliminar (para historial)
            group_snapshot = await menu_history_service.get_modifier_group_snapshot(conn, group_id, tenant_id)
            group_name = group_exists['name']
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

            # Start transaction
            async with conn.transaction():
                # 1. Registrar eliminación en historial
                if group_snapshot:
                    await menu_history_service.record_modifier_group_delete(
                        conn, tenant_id, group_id, group_name,
                        group_snapshot, user_id
                    )

                # 2. Delete product associations first
                delete_assoc_query = "DELETE FROM product_modifier_groups WHERE modifier_group_id = $1"
                await conn.execute(delete_assoc_query, group_id)

                # 3. Delete modifiers (foreign key constraint)
                delete_modifiers_query = "DELETE FROM modifiers WHERE modifier_group_id = $1"
                await conn.execute(delete_modifiers_query, group_id)

                # 4. Delete group
                delete_group_query = "DELETE FROM modifier_groups WHERE id = $1 AND tenant_id = $2"
                await conn.execute(delete_group_query, group_id, tenant_id)

                return {
                    "success": True,
                    "message": "Modifier group deleted successfully"
                }

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting modifier group: {str(e)}")
        raise APIError(f"Error deleting modifier group: {str(e)}", status_code=500)
