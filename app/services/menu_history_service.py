"""
Menu History Service
Registra cambios en productos, recetas y modificadores para trazabilidad y análisis.
"""
import json
import logging
from typing import Optional, Any, Dict, List
from uuid import UUID
from decimal import Decimal

logger = logging.getLogger(__name__)


def _serialize_value(value: Any) -> Any:
    """Serializa valores para JSON (maneja UUID, Decimal, etc.)"""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return value


def _to_jsonb(data: Any) -> Optional[str]:
    """Convierte datos a formato JSON string para JSONB"""
    if data is None:
        return None
    serialized = _serialize_value(data)
    return json.dumps(serialized)


# =====================================================
# HISTORIAL DE PRODUCTOS
# =====================================================

async def record_product_create(
    conn,
    tenant_id: UUID,
    product_id: UUID,
    product_name: str,
    product_snapshot: Dict,
    changed_by: Optional[UUID] = None
):
    """Registra la creación de un producto"""
    try:
        await conn.execute("""
            INSERT INTO product_change_history (
                tenant_id, product_id, product_name,
                change_type, new_value, product_snapshot, changed_by
            ) VALUES ($1, $2, $3, 'create', $4, $5, $6)
        """,
            tenant_id,
            product_id,
            product_name,
            _to_jsonb(product_snapshot),
            _to_jsonb(product_snapshot),
            changed_by
        )
        logger.info(f"Product create recorded: {product_name} ({product_id})")
    except Exception as e:
        logger.error(f"Error recording product create: {e}")
        # No lanzamos excepción para no afectar la operación principal


async def record_product_update(
    conn,
    tenant_id: UUID,
    product_id: UUID,
    product_name: str,
    field_changed: str,
    old_value: Any,
    new_value: Any,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra un cambio específico en un producto"""
    try:
        await conn.execute("""
            INSERT INTO product_change_history (
                tenant_id, product_id, product_name,
                change_type, field_changed, old_value, new_value,
                changed_by, reason
            ) VALUES ($1, $2, $3, 'update', $4, $5, $6, $7, $8)
        """,
            tenant_id,
            product_id,
            product_name,
            field_changed,
            _to_jsonb({field_changed: old_value}),
            _to_jsonb({field_changed: new_value}),
            changed_by,
            reason
        )
        logger.info(f"Product update recorded: {product_name}.{field_changed}")
    except Exception as e:
        logger.error(f"Error recording product update: {e}")


async def record_product_delete(
    conn,
    tenant_id: UUID,
    product_id: UUID,
    product_name: str,
    product_snapshot: Dict,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra la eliminación de un producto"""
    try:
        await conn.execute("""
            INSERT INTO product_change_history (
                tenant_id, product_id, product_name,
                change_type, old_value, product_snapshot, changed_by, reason
            ) VALUES ($1, $2, $3, 'delete', $4, $5, $6, $7)
        """,
            tenant_id,
            product_id,
            product_name,
            _to_jsonb(product_snapshot),
            _to_jsonb(product_snapshot),
            changed_by,
            reason
        )
        logger.info(f"Product delete recorded: {product_name} ({product_id})")
    except Exception as e:
        logger.error(f"Error recording product delete: {e}")


async def compare_and_record_product_changes(
    conn,
    tenant_id: UUID,
    product_id: UUID,
    product_name: str,
    old_data: Dict,
    new_data: Dict,
    changed_by: Optional[UUID] = None
):
    """
    Compara datos antiguos y nuevos, registra cada campo que cambió.
    Campos a comparar: price, name, description, category_id, is_available,
    ingredients, recipe_base_ids, allow_modifiers, preparation_time
    """
    fields_to_track = [
        'price', 'name', 'description', 'category_id', 'is_available',
        'allow_modifiers', 'preparation_time', 'product_base_type_id'
    ]

    for field in fields_to_track:
        old_val = old_data.get(field)
        new_val = new_data.get(field)

        # Normalizar para comparación
        if isinstance(old_val, Decimal):
            old_val = float(old_val)
        if isinstance(new_val, Decimal):
            new_val = float(new_val)
        if isinstance(old_val, UUID):
            old_val = str(old_val)
        if isinstance(new_val, UUID):
            new_val = str(new_val)

        if old_val != new_val and new_val is not None:
            await record_product_update(
                conn, tenant_id, product_id, product_name,
                field, old_val, new_val, changed_by
            )

    # Comparar ingredientes (lista)
    old_ingredients = old_data.get('ingredients', [])
    new_ingredients = new_data.get('ingredients', [])
    if _ingredients_changed(old_ingredients, new_ingredients):
        await record_product_update(
            conn, tenant_id, product_id, product_name,
            'ingredients', old_ingredients, new_ingredients, changed_by
        )

    # Comparar recipe bases (lista)
    old_bases = old_data.get('recipe_base_ids', [])
    new_bases = new_data.get('recipe_base_ids', [])
    if set(str(x) for x in old_bases) != set(str(x) for x in new_bases):
        await record_product_update(
            conn, tenant_id, product_id, product_name,
            'recipe_base_ids', old_bases, new_bases, changed_by
        )


def _ingredients_changed(old_list: List, new_list: List) -> bool:
    """Compara si los ingredientes cambiaron"""
    if len(old_list) != len(new_list):
        return True

    old_set = {(str(i.get('ingredient_id')), float(i.get('quantity', 0)), i.get('unit', ''))
               for i in old_list}
    new_set = {(str(i.get('ingredient_id')), float(i.get('quantity', 0)), i.get('unit', ''))
               for i in new_list}

    return old_set != new_set


# =====================================================
# HISTORIAL DE RECETAS BASE
# =====================================================

async def record_recipe_base_create(
    conn,
    tenant_id: UUID,
    recipe_base_id: UUID,
    recipe_name: str,
    recipe_snapshot: Dict,
    changed_by: Optional[UUID] = None
):
    """Registra la creación de una receta base"""
    try:
        await conn.execute("""
            INSERT INTO recipe_base_change_history (
                tenant_id, recipe_base_id, recipe_base_name,
                change_type, new_value, recipe_snapshot, changed_by
            ) VALUES ($1, $2, $3, 'create', $4, $5, $6)
        """,
            tenant_id,
            recipe_base_id,
            recipe_name,
            _to_jsonb(recipe_snapshot),
            _to_jsonb(recipe_snapshot),
            changed_by
        )
        logger.info(f"Recipe base create recorded: {recipe_name} ({recipe_base_id})")
    except Exception as e:
        logger.error(f"Error recording recipe base create: {e}")


async def record_recipe_base_update(
    conn,
    tenant_id: UUID,
    recipe_base_id: UUID,
    recipe_name: str,
    field_changed: str,
    old_value: Any,
    new_value: Any,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra un cambio específico en una receta base"""
    try:
        await conn.execute("""
            INSERT INTO recipe_base_change_history (
                tenant_id, recipe_base_id, recipe_base_name,
                change_type, field_changed, old_value, new_value,
                changed_by, reason
            ) VALUES ($1, $2, $3, 'update', $4, $5, $6, $7, $8)
        """,
            tenant_id,
            recipe_base_id,
            recipe_name,
            field_changed,
            _to_jsonb({field_changed: old_value}),
            _to_jsonb({field_changed: new_value}),
            changed_by,
            reason
        )
        logger.info(f"Recipe base update recorded: {recipe_name}.{field_changed}")
    except Exception as e:
        logger.error(f"Error recording recipe base update: {e}")


async def record_recipe_base_delete(
    conn,
    tenant_id: UUID,
    recipe_base_id: UUID,
    recipe_name: str,
    recipe_snapshot: Dict,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra la eliminación de una receta base"""
    try:
        await conn.execute("""
            INSERT INTO recipe_base_change_history (
                tenant_id, recipe_base_id, recipe_base_name,
                change_type, old_value, recipe_snapshot, changed_by, reason
            ) VALUES ($1, $2, $3, 'delete', $4, $5, $6, $7)
        """,
            tenant_id,
            recipe_base_id,
            recipe_name,
            _to_jsonb(recipe_snapshot),
            _to_jsonb(recipe_snapshot),
            changed_by,
            reason
        )
        logger.info(f"Recipe base delete recorded: {recipe_name} ({recipe_base_id})")
    except Exception as e:
        logger.error(f"Error recording recipe base delete: {e}")


async def compare_and_record_recipe_base_changes(
    conn,
    tenant_id: UUID,
    recipe_base_id: UUID,
    recipe_name: str,
    old_data: Dict,
    new_data: Dict,
    changed_by: Optional[UUID] = None
):
    """Compara y registra cambios en receta base"""
    fields_to_track = ['name', 'description', 'is_active']

    for field in fields_to_track:
        old_val = old_data.get(field)
        new_val = new_data.get(field)

        if old_val != new_val and new_val is not None:
            await record_recipe_base_update(
                conn, tenant_id, recipe_base_id, recipe_name,
                field, old_val, new_val, changed_by
            )

    # Comparar ingredientes
    old_ingredients = old_data.get('ingredients', [])
    new_ingredients = new_data.get('ingredients', [])
    if _recipe_ingredients_changed(old_ingredients, new_ingredients):
        await record_recipe_base_update(
            conn, tenant_id, recipe_base_id, recipe_name,
            'ingredients', old_ingredients, new_ingredients, changed_by
        )


def _recipe_ingredients_changed(old_list: List, new_list: List) -> bool:
    """Compara si los ingredientes de receta base cambiaron"""
    if len(old_list) != len(new_list):
        return True

    old_set = {(str(i.get('ingredient_id')), float(i.get('base_quantity', 0)), i.get('unit', ''))
               for i in old_list}
    new_set = {(str(i.get('ingredient_id')), float(i.get('base_quantity', 0)), i.get('unit', ''))
               for i in new_list}

    return old_set != new_set


# =====================================================
# HISTORIAL DE MODIFICADORES
# =====================================================

async def record_modifier_group_create(
    conn,
    tenant_id: UUID,
    modifier_group_id: UUID,
    group_name: str,
    group_snapshot: Dict,
    changed_by: Optional[UUID] = None
):
    """Registra la creación de un grupo de modificadores"""
    try:
        await conn.execute("""
            INSERT INTO modifier_change_history (
                tenant_id, entity_type, modifier_group_id, entity_name,
                change_type, new_value, modifier_snapshot, changed_by
            ) VALUES ($1, 'modifier_group', $2, $3, 'create', $4, $5, $6)
        """,
            tenant_id,
            modifier_group_id,
            group_name,
            _to_jsonb(group_snapshot),
            _to_jsonb(group_snapshot),
            changed_by
        )
        logger.info(f"Modifier group create recorded: {group_name} ({modifier_group_id})")
    except Exception as e:
        logger.error(f"Error recording modifier group create: {e}")


async def record_modifier_group_update(
    conn,
    tenant_id: UUID,
    modifier_group_id: UUID,
    group_name: str,
    field_changed: str,
    old_value: Any,
    new_value: Any,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra un cambio específico en un grupo de modificadores"""
    try:
        await conn.execute("""
            INSERT INTO modifier_change_history (
                tenant_id, entity_type, modifier_group_id, entity_name,
                change_type, field_changed, old_value, new_value,
                changed_by, reason
            ) VALUES ($1, 'modifier_group', $2, $3, 'update', $4, $5, $6, $7, $8)
        """,
            tenant_id,
            modifier_group_id,
            group_name,
            field_changed,
            _to_jsonb({field_changed: old_value}),
            _to_jsonb({field_changed: new_value}),
            changed_by,
            reason
        )
        logger.info(f"Modifier group update recorded: {group_name}.{field_changed}")
    except Exception as e:
        logger.error(f"Error recording modifier group update: {e}")


async def record_modifier_group_delete(
    conn,
    tenant_id: UUID,
    modifier_group_id: UUID,
    group_name: str,
    group_snapshot: Dict,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra la eliminación de un grupo de modificadores"""
    try:
        await conn.execute("""
            INSERT INTO modifier_change_history (
                tenant_id, entity_type, modifier_group_id, entity_name,
                change_type, old_value, modifier_snapshot, changed_by, reason
            ) VALUES ($1, 'modifier_group', $2, $3, 'delete', $4, $5, $6, $7)
        """,
            tenant_id,
            modifier_group_id,
            group_name,
            _to_jsonb(group_snapshot),
            _to_jsonb(group_snapshot),
            changed_by,
            reason
        )
        logger.info(f"Modifier group delete recorded: {group_name} ({modifier_group_id})")
    except Exception as e:
        logger.error(f"Error recording modifier group delete: {e}")


async def record_modifier_update(
    conn,
    tenant_id: UUID,
    modifier_id: UUID,
    modifier_name: str,
    field_changed: str,
    old_value: Any,
    new_value: Any,
    changed_by: Optional[UUID] = None,
    reason: Optional[str] = None
):
    """Registra un cambio específico en un modificador individual"""
    try:
        await conn.execute("""
            INSERT INTO modifier_change_history (
                tenant_id, entity_type, modifier_id, entity_name,
                change_type, field_changed, old_value, new_value,
                changed_by, reason
            ) VALUES ($1, 'modifier', $2, $3, 'update', $4, $5, $6, $7, $8)
        """,
            tenant_id,
            modifier_id,
            modifier_name,
            field_changed,
            _to_jsonb({field_changed: old_value}),
            _to_jsonb({field_changed: new_value}),
            changed_by,
            reason
        )
        logger.info(f"Modifier update recorded: {modifier_name}.{field_changed}")
    except Exception as e:
        logger.error(f"Error recording modifier update: {e}")


async def compare_and_record_modifier_group_changes(
    conn,
    tenant_id: UUID,
    modifier_group_id: UUID,
    group_name: str,
    old_data: Dict,
    new_data: Dict,
    changed_by: Optional[UUID] = None
):
    """Compara y registra cambios en grupo de modificadores"""
    fields_to_track = ['name', 'min_qty', 'max_qty', 'is_required', 'sort_order']

    for field in fields_to_track:
        old_val = old_data.get(field)
        new_val = new_data.get(field)

        if old_val != new_val and new_val is not None:
            await record_modifier_group_update(
                conn, tenant_id, modifier_group_id, group_name,
                field, old_val, new_val, changed_by
            )

    # Comparar productos asociados
    old_products = set(str(p) for p in old_data.get('product_ids', []))
    new_products = set(str(p) for p in new_data.get('product_ids', []))
    if old_products != new_products:
        await record_modifier_group_update(
            conn, tenant_id, modifier_group_id, group_name,
            'product_ids', list(old_products), list(new_products), changed_by
        )

    # Comparar modificadores individuales
    old_modifiers = old_data.get('modifiers', [])
    new_modifiers = new_data.get('modifiers', [])
    if _modifiers_changed(old_modifiers, new_modifiers):
        await record_modifier_group_update(
            conn, tenant_id, modifier_group_id, group_name,
            'modifiers', old_modifiers, new_modifiers, changed_by
        )


def _modifiers_changed(old_list: List, new_list: List) -> bool:
    """Compara si los modificadores cambiaron"""
    if len(old_list) != len(new_list):
        return True

    def modifier_key(m):
        return (
            str(m.get('id', '')),
            m.get('name', ''),
            float(m.get('price', 0)),
            m.get('max_limit', 1),
            m.get('included_quantity', 0),
            m.get('is_available', True),
            str(m.get('ingredient_id', '')),
            float(m.get('ingredient_quantity', 0)) if m.get('ingredient_quantity') else 0
        )

    old_set = {modifier_key(m) for m in old_list}
    new_set = {modifier_key(m) for m in new_list}

    return old_set != new_set


# =====================================================
# HELPERS PARA OBTENER SNAPSHOTS
# =====================================================

async def get_product_snapshot(conn, product_id: UUID, tenant_id: UUID) -> Optional[Dict]:
    """Obtiene un snapshot completo del producto actual"""
    try:
        # Producto base
        product = await conn.fetchrow("""
            SELECT id, name, description, price, category_id, is_available,
                   allow_modifiers, preparation_time, product_base_type_id,
                   costo_calculado, costo_percibido, controla_stock, is_combo
            FROM product WHERE id = $1 AND tenant_id = $2
        """, product_id, tenant_id)

        if not product:
            return None

        snapshot = dict(product)

        # Ingredientes
        ingredients = await conn.fetch("""
            SELECT ingredient_id, quantity, unit
            FROM product_recipes WHERE product_id = $1
        """, product_id)
        snapshot['ingredients'] = [dict(i) for i in ingredients]

        # Recipe bases (Issue #517: snapshot includes per-product quantity)
        bases = await conn.fetch("""
            SELECT product_base_type_id, quantity
            FROM product_base_recipes WHERE product_id = $1
        """, product_id)
        snapshot['recipe_base_ids'] = [b['product_base_type_id'] for b in bases]
        snapshot['recipe_bases'] = [
            {'recipe_base_id': b['product_base_type_id'], 'quantity': b['quantity']}
            for b in bases
        ]

        return snapshot
    except Exception as e:
        logger.error(f"Error getting product snapshot: {e}")
        return None


async def get_recipe_base_snapshot(conn, recipe_base_id: UUID, tenant_id: UUID) -> Optional[Dict]:
    """Obtiene un snapshot completo de la receta base actual"""
    try:
        recipe = await conn.fetchrow("""
            SELECT id, name, description, is_active
            FROM product_base_types WHERE id = $1 AND tenant_id = $2
        """, recipe_base_id, tenant_id)

        if not recipe:
            return None

        snapshot = dict(recipe)

        # Ingredientes
        ingredients = await conn.fetch("""
            SELECT ingredient_id, base_quantity, unit, is_required, notes
            FROM base_recipe_templates WHERE product_base_type_id = $1
        """, recipe_base_id)
        snapshot['ingredients'] = [dict(i) for i in ingredients]

        return snapshot
    except Exception as e:
        logger.error(f"Error getting recipe base snapshot: {e}")
        return None


async def get_modifier_group_snapshot(conn, modifier_group_id: UUID, tenant_id: UUID) -> Optional[Dict]:
    """Obtiene un snapshot completo del grupo de modificadores actual"""
    try:
        group = await conn.fetchrow("""
            SELECT id, name, min_qty, max_qty, is_required, sort_order
            FROM modifier_groups WHERE id = $1 AND tenant_id = $2
        """, modifier_group_id, tenant_id)

        if not group:
            return None

        snapshot = dict(group)

        # Productos asociados
        products = await conn.fetch("""
            SELECT product_id FROM product_modifier_groups WHERE modifier_group_id = $1
        """, modifier_group_id)
        snapshot['product_ids'] = [p['product_id'] for p in products]

        # Modificadores
        modifiers = await conn.fetch("""
            SELECT id, name, price, max_limit, included_quantity,
                   is_available, is_default, sort_order,
                   option_type,
                   ingredient_id, ingredient_quantity, ingredient_unit,
                   recipe_base_type_id, recipe_base_quantity,
                   linked_product_id, linked_product_quantity
            FROM modifiers WHERE modifier_group_id = $1
        """, modifier_group_id)
        recipe_lines = await conn.fetch("""
            SELECT mr.modifier_id, mr.ingredient_id, mr.quantity, mr.unit
            FROM modifier_recipes mr
            JOIN modifiers m ON m.id = mr.modifier_id
            WHERE m.modifier_group_id = $1
            ORDER BY mr.modifier_id, mr.created_at, mr.id
        """, modifier_group_id)
        lines_by_modifier: Dict[UUID, List[Dict]] = {}
        for line in recipe_lines:
            modifier_id = line["modifier_id"]
            lines_by_modifier.setdefault(modifier_id, []).append({
                "ingredient_id": line["ingredient_id"],
                "quantity": line["quantity"],
                "unit": line["unit"],
            })

        snapshot['modifiers'] = [
            {
                **dict(modifier),
                "recipe_lines": lines_by_modifier.get(modifier["id"], []),
            }
            for modifier in modifiers
        ]

        return snapshot
    except Exception as e:
        logger.error(f"Error getting modifier group snapshot: {e}")
        return None
