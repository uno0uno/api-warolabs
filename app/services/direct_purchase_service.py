"""
Direct Purchase Service
Handles the simplified flow for immediate stock updates (Compras Directas)
"""

from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

_DIRECT_PURCHASE_DECIMAL_SCALE = Decimal("0.000000000000001")
_DIRECT_PURCHASE_DECIMAL_PRECISION = 42
_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE = Decimal("1")

# Catalog units matching backend PURCHASE_UNIT_CATALOG
# Used to convert catalog keys (lt, kg, galon…) to base units (ml, gr)
_CATALOG_TO_BASE: Dict[str, Any] = {
    'kg':         {'factor': 1000,  'base': 'gr'},
    'libra':      {'factor': 500,   'base': 'gr'},
    'arroba':     {'factor': 12500, 'base': 'gr'},
    'bulto_25kg': {'factor': 25000, 'base': 'gr'},
    'lt':         {'factor': 1000,  'base': 'ml'},
    'botella':    {'factor': 750,   'base': 'ml'},
    'galon':      {'factor': 3785,  'base': 'ml'},
    'und':        {'factor': 1,     'base': 'und'},
}


def _direct_purchase_decimal(value: Any, default: Decimal = _DECIMAL_ZERO) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantize_direct_purchase_decimal(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _DIRECT_PURCHASE_DECIMAL_PRECISION
        return value.quantize(_DIRECT_PURCHASE_DECIMAL_SCALE)


def _has_direct_purchase_value(value: Any) -> bool:
    return value is not None and value != ""


def _direct_purchase_item_total(item: Dict[str, Any]) -> Decimal:
    if _has_direct_purchase_value(item.get('total_cost')):
        return _direct_purchase_decimal(item.get('total_cost'))

    quantity = _direct_purchase_decimal(
        item.get('purchase_quantity'),
        _direct_purchase_decimal(item.get('quantity')),
    )
    unit_cost = _direct_purchase_decimal(item.get('unit_cost'))
    return quantity * unit_cost


def _catalog_direct_purchase_conversion_factor(
    purchase_unit: Optional[str],
    base_unit: str,
    ingredient: Any,
) -> Optional[Decimal]:
    catalog_entry = _CATALOG_TO_BASE.get(purchase_unit or "")
    if not catalog_entry:
        return None

    cat_factor = _direct_purchase_decimal(catalog_entry['factor'])
    cat_base = catalog_entry['base']
    ingredient_weight = _direct_purchase_decimal(ingredient['unit_weight_gr'])
    ing_weight_unit = ingredient['unit_weight_unit'] or ''

    if base_unit == 'und' and cat_base == ing_weight_unit and ingredient_weight > 0:
        return _quantize_direct_purchase_decimal(cat_factor / ingredient_weight)
    if cat_base == base_unit:
        return _quantize_direct_purchase_decimal(cat_factor)
    return None


def _resolve_direct_purchase_item_decimals(
    item: Dict[str, Any],
    base_unit: str,
    purchase_unit: Optional[str],
    conversion_factor: Decimal,
) -> Dict[str, Decimal]:
    quantity = _direct_purchase_decimal(item.get('quantity'))
    purchase_quantity = _direct_purchase_decimal(
        item.get('purchase_quantity'),
        quantity,
    )
    item_total = _direct_purchase_item_total(item)
    unit_cost = _direct_purchase_decimal(item.get('unit_cost'))

    if purchase_quantity > 0 and _has_direct_purchase_value(item.get('total_cost')):
        unit_cost = item_total / purchase_quantity

    base_quantity = purchase_quantity * conversion_factor
    base_unit_cost = (
        unit_cost / conversion_factor
        if conversion_factor > 0
        else unit_cost
    )

    return {
        "purchase_quantity": _quantize_direct_purchase_decimal(purchase_quantity),
        "conversion_factor": _quantize_direct_purchase_decimal(conversion_factor),
        "base_quantity": _quantize_direct_purchase_decimal(base_quantity),
        "base_unit_cost": _quantize_direct_purchase_decimal(base_unit_cost),
        "item_total": _quantize_direct_purchase_decimal(item_total),
    }


def _calculate_direct_purchase_total(items: List[Dict[str, Any]]) -> Decimal:
    return _quantize_direct_purchase_decimal(
        sum((_direct_purchase_item_total(item) for item in items), _DECIMAL_ZERO)
    )


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO date string, handling JS-style 'Z' timezone suffix."""
    if not date_str:
        return None
    return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
from fastapi import Request, Response, HTTPException, UploadFile
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.core.timezones import local_date_for_tenant, resolve_tenant_timezone
from app.services.purchase_tracking_service import (
    create_status_history_entry,
    upload_purchase_attachments
)
from app.services.billing_service import check_plan_quota_period
from app.services.account_role_service import (
    AccountRole,
    MissingAccountRoleError,
    resolve_account,
    resolve_payment_account,
)
import logging
import json
from uuid import uuid4

logger = logging.getLogger(__name__)

CONTADO_REQUIRES_PAYMENT_METHOD_DETAIL = (
    "Contado requires a payment method. Use credito when payment is not registered yet."
)


def assert_contado_requires_payment_method(
    payment_type: Optional[str],
    payment_method: Optional[str],
) -> None:
    """Contado = paid now; unpaid purchases must use credito (or other deferred type)."""
    if (payment_type or "").strip().lower() != "contado":
        return
    if payment_method and str(payment_method).strip():
        return
    raise HTTPException(status_code=400, detail=CONTADO_REQUIRES_PAYMENT_METHOD_DETAIL)


async def _normalize_direct_purchase_payment(
    conn,
    payment_method: Optional[str],
    payment_method_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve group slug + optional sub-method UUID for tenant_purchases."""
    if payment_method_id:
        row = await conn.fetchrow(
            """
            SELECT pmg.slug
            FROM payment_methods pm
            JOIN payment_method_groups pmg ON pmg.id = pm.group_id
            WHERE pm.id = $1::uuid
            """,
            payment_method_id,
        )
        if row:
            return row["slug"], payment_method_id
        return payment_method, payment_method_id

    if payment_method and len(payment_method) == 36 and "-" in payment_method:
        row = await conn.fetchrow(
            """
            SELECT pmg.slug
            FROM payment_methods pm
            JOIN payment_method_groups pmg ON pmg.id = pm.group_id
            WHERE pm.id = $1::uuid
            """,
            payment_method,
        )
        if row:
            return row["slug"], payment_method

    return payment_method, None


# ---------------------------------------------------------------------------
# GL helper — Auto-posting compras-directas → GL (#106)
# ---------------------------------------------------------------------------

async def _post_purchase_gl_entry(
    conn,
    tenant_id: UUID,
    purchase_id: UUID,
    total_amount: float,
    purchase_date,            # datetime or date
    description: str,
    payment_method: Optional[str],
    payment_method_id: Optional[UUID],
    timezone_name: str,
) -> None:
    """
    Post GL entry for a received purchase:
        Déb INVENTORY          ←  total_amount
        Cré <payment account>  →  total_amount

    Credit account resolution (two-level, migration 028):
        1. payment_methods.gl_account_code       (individual override)
        2. payment_method_groups.gl_account_code  (group default)
        3. payment_method group slug default      (cash/card/digital without sub-method)
        4. ACCOUNTS_PAYABLE role when no payment method is set

    Skips zero amounts and closed periods. Missing roles fail explicitly.
    """
    amount = Decimal(str(total_amount))
    if amount <= 0:
        logger.info(f"[GL] Purchase {purchase_id}: zero amount — skip GL post")
        return

    entry_date = local_date_for_tenant(purchase_date, timezone_name)
    period_year = entry_date.year
    period_month = entry_date.month

    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, period_year, period_month,
    )
    if closed:
        logger.warning(
            f"[GL] Period {period_year}-{period_month:02d} closed — "
            f"skip GL post for purchase {purchase_id}"
        )
        return

    debit_acct = await resolve_account(
        conn, tenant_id, AccountRole.INVENTORY, source="direct_purchase"
    )
    if payment_method or payment_method_id:
        credit_acct = await resolve_payment_account(
            conn,
            tenant_id,
            payment_method,
            payment_method_id=payment_method_id,
            source="direct_purchase",
        )
    else:
        credit_acct = await resolve_account(
            conn, tenant_id, AccountRole.ACCOUNTS_PAYABLE, source="direct_purchase"
        )

    amt = float(amount)
    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'inventario', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry_date, period_year, period_month,
            description, purchase_id, amt, amt,
        )
        entry_id = entry_row["id"]
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, debit_acct.id, amt, description,
        )
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, credit_acct.id, amt, description,
        )

    logger.info(
        f"[GL] ✅ Posted purchase entry {entry_id} for purchase {purchase_id} "
        f"(debit={debit_acct.code}, credit={credit_acct.code}, amount={amt})"
    )


def calculate_changes_summary(before: List[Dict], after: List[Dict]) -> Dict:
    """
    Genera resumen legible de cambios entre items antes y después de una edición.

    Args:
        before: Lista de items antes de la edición
        after: Lista de items después de la edición

    Returns:
        Dict con listas de items added, removed, modified
    """
    # Crear mapas por ingredient_id para comparación rápida
    before_map = {str(item['ingredient_id']): item for item in before}
    after_map = {str(item['ingredient_id']): item for item in after}

    added = []
    removed = []
    modified = []

    # Items agregados (están en after pero no en before)
    for ing_id, item in after_map.items():
        if ing_id not in before_map:
            qty = item.get('purchase_quantity') or item.get('quantity', 0)
            unit = item.get('purchase_unit') or item.get('unit', '')
            name = item.get('ingredient_name', 'Item')
            added.append(f"{name} x {qty} {unit}")

    # Items eliminados (están en before pero no en after)
    for ing_id, item in before_map.items():
        if ing_id not in after_map:
            qty = item.get('purchase_quantity') or item.get('quantity', 0)
            unit = item.get('purchase_unit') or item.get('unit', '')
            name = item.get('ingredient_name', 'Item')
            removed.append(f"{name} x {qty} {unit}")

    # Items modificados (están en ambos pero con cambios)
    for ing_id, item_after in after_map.items():
        if ing_id in before_map:
            item_before = before_map[ing_id]

            qty_before = float(item_before.get('purchase_quantity') or item_before.get('quantity', 0))
            qty_after = float(item_after.get('purchase_quantity') or item_after.get('quantity', 0))
            cost_before = float(item_before.get('unit_cost', 0))
            cost_after = float(item_after.get('unit_cost', 0))
            unit_before = item_before.get('purchase_unit') or item_before.get('unit', '')
            unit_after = item_after.get('purchase_unit') or item_after.get('unit', '')
            name = item_after.get('ingredient_name', 'Item')

            # Verificar si hubo cambios significativos
            if (abs(qty_before - qty_after) > 0.001 or
                abs(cost_before - cost_after) > 0.001 or
                unit_before != unit_after):

                changes = []
                if abs(qty_before - qty_after) > 0.001 or unit_before != unit_after:
                    changes.append(f"{qty_before} {unit_before} → {qty_after} {unit_after}")
                if abs(cost_before - cost_after) > 0.001:
                    changes.append(f"${cost_before:,.0f} → ${cost_after:,.0f}")

                if changes:
                    modified.append(f"{name}: {', '.join(changes)}")

    return {"added": added, "removed": removed, "modified": modified}


async def get_next_direct_purchase_number(conn, tenant_id: UUID) -> str:
    """Generate the next direct purchase number for the tenant (WR-CD-YYYY-XXXX format)"""
    current_year = datetime.now().year
    prefix = f'WR-CD-{current_year}-'

    last_purchase = await conn.fetchrow("""
        SELECT purchase_number
        FROM tenant_purchases
        WHERE tenant_id = $1 AND purchase_number LIKE $2
        ORDER BY purchase_number DESC
        LIMIT 1
    """, tenant_id, f'{prefix}%')

    if last_purchase and last_purchase['purchase_number']:
        try:
            last_number = int(last_purchase['purchase_number'].split('-')[-1])
            next_number = last_number + 1
        except (ValueError, IndexError):
            next_number = 1
    else:
        next_number = 1

    return f"{prefix}{next_number:04d}"


async def create_direct_purchase(
    request: Request,
    response: Response,
    supplier_id: UUID,
    items_data: str,
    new_units_data: Optional[str] = None,
    payment_type: str = "contado",
    payment_terms: Optional[str] = None,
    notes: Optional[str] = None,
    invoice_number: Optional[str] = None,
    invoice_amount: Optional[float] = None,
    invoice_date: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    payment_reference: Optional[str] = None,
    payment_amount: Optional[float] = None,
    payment_date: Optional[str] = None,
    purchase_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a direct purchase that immediately updates inventory.

    This is a simplified flow that:
    1. Creates purchase with status 'received' and is_direct_entry=True
    2. Inserts items with prices
    3. Updates inventory immediately
    4. Records inventory movements
    5. Optionally attaches invoice and payment proof
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse items data from JSON string
        try:
            items = json.loads(items_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid items data format")

        if not items or len(items) == 0:
            raise HTTPException(status_code=400, detail="At least one item is required")

        assert_contado_requires_payment_method(payment_type, payment_method)

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            async with conn.transaction():
                await check_plan_quota_period(
                    conn, tenant_id, "direct_purchases_per_period"
                )
                # 1. Generate purchase number with WR-CD prefix
                purchase_number = await get_next_direct_purchase_number(conn, tenant_id)

                payment_method, payment_method_id = await _normalize_direct_purchase_payment(
                    conn,
                    payment_method,
                    payment_method_id,
                )
                assert_contado_requires_payment_method(payment_type, payment_method)

                # 2. Calculate totals from items
                total_amount = _calculate_direct_purchase_total(items)

                # Determine final status based on payment info
                final_status = 'received'
                if payment_method and payment_amount:
                    final_status = 'paid'
                elif invoice_number:
                    final_status = 'invoiced'

                # 3. Create purchase record
                purchase_row = await conn.fetchrow("""
                    INSERT INTO tenant_purchases (
                        tenant_id,
                        supplier_id,
                        purchase_number,
                        purchase_date,
                        total_amount,
                        tax_amount,
                        status,
                        invoice_number,
                        invoice_date,
                        invoice_amount,
                        notes,
                        created_by,
                        payment_type,
                        payment_terms,
                        payment_method,
                        payment_method_id,
                        payment_reference,
                        payment_amount,
                        payment_date,
                        is_direct_entry,
                        received_at,
                        received_by,
                        paid_at
                    ) VALUES (
                        $1, $2, $3, COALESCE($20, NOW()), $4, 0, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14::uuid, $15, $16, $17, TRUE, NOW(), $18,
                        CASE WHEN $19 THEN NOW() ELSE NULL END
                    )
                    RETURNING id, purchase_date
                """,
                    tenant_id,
                    supplier_id,
                    purchase_number,
                    total_amount,
                    final_status,
                    invoice_number,
                    _parse_date(invoice_date),
                    invoice_amount,
                    notes,
                    user_id,
                    payment_type,
                    payment_terms,
                    payment_method,
                    payment_method_id,
                    payment_reference,
                    payment_amount,
                    _parse_date(payment_date),
                    user_id,
                    bool(payment_method and payment_amount),
                    _parse_date(purchase_date)
                )

                purchase_id = purchase_row['id']

                # 4. Create any new purchase unit presentations bundled with this purchase
                if new_units_data:
                    try:
                        new_units = json.loads(new_units_data)
                        for new_unit in new_units:
                            new_ing_id = UUID(str(new_unit['ingredient_id']))
                            await conn.execute("""
                                INSERT INTO ingredient_purchase_units (
                                    ingredient_id,
                                    purchase_unit,
                                    purchase_unit_label,
                                    conversion_factor,
                                    is_active,
                                    is_default
                                )
                                SELECT $1::uuid, $2::varchar, $3::varchar, $4::numeric, TRUE, FALSE
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM ingredient_purchase_units
                                    WHERE ingredient_id = $1::uuid AND purchase_unit_label = $3::varchar
                                )
                            """,
                                new_ing_id,
                                str(new_unit['purchase_unit']),
                                str(new_unit['purchase_unit_label']),
                                _direct_purchase_decimal(new_unit['conversion_factor'])
                            )
                            logger.info(f"Created purchase unit: {new_unit['purchase_unit_label']} for ingredient {new_ing_id}")
                    except (json.JSONDecodeError, KeyError, ValueError, InvalidOperation) as e:
                        logger.warning(f"Error processing new_units_data: {e}")

                # 5. Insert items and update inventory
                for item in items:
                    ingredient_id_str = item.get('ingredient_id')

                    try:
                        ingredient_id = UUID(ingredient_id_str) if isinstance(ingredient_id_str, str) else ingredient_id_str
                    except (ValueError, TypeError):
                        logger.error(f"Invalid ingredient_id format: {ingredient_id_str}")
                        continue

                    # Get ingredient base unit and weight info for catalog conversions
                    ingredient = await conn.fetchrow("""
                        SELECT unit, unit_weight_gr, unit_weight_unit FROM ingredients WHERE id = $1
                    """, ingredient_id)

                    if not ingredient:
                        logger.warning(f"Ingredient not found: {ingredient_id}")
                        continue

                    base_unit = ingredient['unit']
                    purchase_unit = item.get('purchase_unit', base_unit)

                    # Get conversion factor if purchase unit differs from base unit
                    conversion_factor = _DECIMAL_ONE
                    if purchase_unit and purchase_unit != base_unit:
                        conversion_row = await conn.fetchrow("""
                            SELECT conversion_factor
                            FROM ingredient_purchase_units
                            WHERE ingredient_id = $1
                              AND purchase_unit_label = $2
                              AND is_active = TRUE
                        """, ingredient_id, purchase_unit)

                        if conversion_row:
                            conversion_factor = _direct_purchase_decimal(conversion_row['conversion_factor'])
                        else:
                            # Fallback: catalog unit (lt, kg, galon…) — two-step conversion for und ingredients
                            catalog_factor = _catalog_direct_purchase_conversion_factor(
                                purchase_unit,
                                base_unit,
                                ingredient,
                            )
                            if catalog_factor is not None:
                                conversion_factor = catalog_factor

                    values = _resolve_direct_purchase_item_decimals(
                        item,
                        base_unit,
                        purchase_unit,
                        conversion_factor,
                    )
                    purchase_quantity = values["purchase_quantity"]
                    conversion_factor = values["conversion_factor"]
                    base_quantity = values["base_quantity"]
                    base_unit_cost = values["base_unit_cost"]
                    item_total = values["item_total"]

                    logger.info(f"Direct purchase item: {purchase_quantity} {purchase_unit} -> {base_quantity} {base_unit} (factor: {conversion_factor})")

                    # Insert purchase item
                    await conn.execute("""
                        INSERT INTO tenant_purchase_items (
                            purchase_id,
                            ingredient_id,
                            quantity,
                            unit,
                            purchase_quantity,
                            purchase_unit,
                            unit_cost,
                            total_cost,
                            quantity_received,
                            received_at,
                            quality_status,
                            item_condition,
                            notes
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), 'good', 'complete', $10)
                    """,
                        purchase_id,
                        ingredient_id,
                        base_quantity,
                        base_unit,
                        purchase_quantity,
                        purchase_unit,
                        base_unit_cost,
                        item_total,
                        base_quantity,  # quantity_received = full quantity
                        item.get('notes')
                    )

                    # Update inventory
                    inventory_row = await conn.fetchrow("""
                        SELECT id, current_stock
                        FROM tenant_inventory
                        WHERE tenant_id = $1 AND ingredient_id = $2
                        FOR UPDATE
                    """, tenant_id, ingredient_id)

                    previous_stock = _DECIMAL_ZERO
                    if inventory_row:
                        previous_stock = _direct_purchase_decimal(inventory_row['current_stock'])
                        new_stock = previous_stock + base_quantity

                        await conn.execute("""
                            UPDATE tenant_inventory
                            SET current_stock = $1, last_updated = NOW()
                            WHERE tenant_id = $2 AND ingredient_id = $3
                        """, new_stock, tenant_id, ingredient_id)
                    else:
                        new_stock = base_quantity
                        await conn.execute("""
                            INSERT INTO tenant_inventory (
                                tenant_id, ingredient_id, current_stock, minimum_stock
                            ) VALUES ($1, $2, $3, 0)
                        """, tenant_id, ingredient_id, new_stock)

                    # Create inventory movement record
                    await conn.execute("""
                        INSERT INTO tenant_ingredient_movements (
                            tenant_id,
                            ingredient_id,
                            movement_type,
                            quantity_change,
                            unit,
                            previous_stock,
                            new_stock,
                            reference_table,
                            reference_id,
                            cost_per_unit,
                            notes,
                            created_by
                        ) VALUES ($1, $2, 'purchase', $3, $4, $5, $6, 'tenant_purchases', $7, $8, $9, $10)
                    """,
                        tenant_id,
                        ingredient_id,
                        base_quantity,
                        base_unit,
                        previous_stock,
                        new_stock,
                        purchase_id,
                        base_unit_cost,
                        f"Compra directa - {purchase_number}",
                        user_id
                    )

                # 5. Create status history
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    None, 'received', user_id,
                    {"direct_entry": True, "items_count": len(items)},
                    "Compra directa - entrada inmediata de stock"
                )

                if final_status == 'invoiced':
                    await create_status_history_entry(
                        conn, purchase_id, tenant_id,
                        'received', 'invoiced', user_id,
                        {"invoice_number": invoice_number},
                        None
                    )
                elif final_status == 'paid':
                    await create_status_history_entry(
                        conn, purchase_id, tenant_id,
                        'received', 'paid', user_id,
                        {
                            "payment_method": payment_method,
                            "payment_amount": str(payment_amount)
                        },
                        None
                    )

                # 6. GL auto-post — Déb INVENTORY / Cré <payment account> (#106)
                #    SAVEPOINT: GL failure never rolls back the purchase save.
                try:
                    async with conn.transaction():
                        await _post_purchase_gl_entry(
                            conn, tenant_id, purchase_id,
                            total_amount,
                            purchase_row['purchase_date'],
                            purchase_number,
                            payment_method,
                            UUID(payment_method_id) if payment_method_id else None,
                            timezone_name,
                        )
                except MissingAccountRoleError:
                    raise
                except Exception as _gl_err:
                    logger.warning(
                        f"[GL] purchase GL post failed for {purchase_id}: {_gl_err}"
                    )

                # 7. Attachments are now uploaded via separate endpoint
                # POST /suppliers/purchases/{purchase_id}/attachments

                return {
                    "success": True,
                    "message": "Compra directa creada exitosamente",
                    "data": {
                        "id": str(purchase_id),
                        "purchase_number": purchase_number,
                        "status": final_status,
                        "total_amount": total_amount,
                        "items_count": len(items),
                        "inventory_updated": True
                    }
                }

    except MissingAccountRoleError:
        raise
    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_direct_purchase: {str(e)}")
        logger.exception(e)
        raise HTTPException(
            status_code=500,
            detail=f"Error creando compra directa: {str(e)}"
        )


async def _get_direct_purchases_for_tenant(
    tenant_id: str,
    *,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    status: Optional[str] = None,
    supplier_id: Optional[UUID] = None,
    date_filter: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get list of direct purchases (is_direct_entry = TRUE) with tenant isolation
    """
    try:
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            # Build query with tenant isolation and is_direct_entry filter
            base_query = """
                SELECT
                    tp.id,
                    tp.tenant_id,
                    tp.supplier_id,
                    tp.purchase_number,
                    tp.purchase_date,
                    tp.total_amount,
                    tp.tax_amount,
                    tp.status,
                    tp.invoice_number,
                    tp.notes,
                    tp.created_by,
                    tp.created_at,
                    tp.updated_at,
                    tp.payment_type,
                    tp.payment_method,
                    tp.payment_method_id::text as payment_method_id,
                    tp.payment_reference,
                    tp.payment_amount,
                    tp.payment_date,
                    tp.paid_at,
                    tp.received_at,
                    tp.is_direct_entry,
                    ts.name as supplier_name
                FROM tenant_purchases tp
                LEFT JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                WHERE tp.tenant_id = $1 AND tp.is_direct_entry = TRUE
            """

            count_query = """
                SELECT COUNT(*) as total
                FROM tenant_purchases tp
                LEFT JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                WHERE tp.tenant_id = $1 AND tp.is_direct_entry = TRUE
            """

            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(tp.purchase_number) LIKE LOWER(${param_count}) OR LOWER(tp.invoice_number) LIKE LOWER(${param_count}) OR LOWER(ts.name) LIKE LOWER(${param_count}))"
                count_query += f" AND (LOWER(tp.purchase_number) LIKE LOWER(${param_count}) OR LOWER(tp.invoice_number) LIKE LOWER(${param_count}) OR LOWER(ts.name) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1

            if status:
                base_query += f" AND LOWER(tp.status) = LOWER(${param_count})"
                count_query += f" AND LOWER(tp.status) = LOWER(${param_count})"
                params.append(status)
                param_count += 1

            if supplier_id:
                base_query += f" AND tp.supplier_id = ${param_count}"
                count_query += f" AND tp.supplier_id = ${param_count}"
                params.append(supplier_id)
                param_count += 1

            # Date filter
            if date_filter in {'today', 'yesterday', 'last_week', '15_days', '1_month', '3_months'}:
                tz_param = param_count
                params.append(timezone_name)
                param_count += 1
                if date_filter == 'today':
                    base_query += f" AND DATE(tp.purchase_date AT TIME ZONE ${tz_param}) = (NOW() AT TIME ZONE ${tz_param})::date"
                    count_query += f" AND DATE(tp.purchase_date AT TIME ZONE ${tz_param}) = (NOW() AT TIME ZONE ${tz_param})::date"
                elif date_filter == 'yesterday':
                    base_query += f" AND DATE(tp.purchase_date AT TIME ZONE ${tz_param}) = (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '1 day'"
                    count_query += f" AND DATE(tp.purchase_date AT TIME ZONE ${tz_param}) = (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '1 day'"
                elif date_filter == 'last_week':
                    base_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '7 days'"
                    count_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '7 days'"
                elif date_filter == '15_days':
                    base_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '15 days'"
                    count_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '15 days'"
                elif date_filter == '1_month':
                    base_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '1 month'"
                    count_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '1 month'"
                elif date_filter == '3_months':
                    base_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '3 months'"
                    count_query += f" AND (tp.purchase_date AT TIME ZONE ${tz_param}) >= (NOW() AT TIME ZONE ${tz_param})::date - INTERVAL '3 months'"

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY tp.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            # Execute queries
            purchases_data = await conn.fetch(base_query, *params)
            count_result = await conn.fetchrow(count_query, *params[:-2])

            # Convert to list of dicts
            purchases = []
            for row in purchases_data:
                purchase = dict(row)
                # Get items count for each purchase
                items_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM tenant_purchase_items WHERE purchase_id = $1
                """, row['id'])
                purchase['items_count'] = items_count
                purchases.append(purchase)

            return {
                "success": True,
                "data": purchases,
                "total": count_result['total'] if count_result else 0,
                "page": page,
                "limit": limit
            }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error in get_direct_purchases_list: {str(e)}")
        logger.exception(e)
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def get_direct_purchases_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    status: Optional[str] = None,
    supplier_id: Optional[UUID] = None,
    date_filter: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get list of direct purchases for the current session tenant.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    return await _get_direct_purchases_for_tenant(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        status=status,
        supplier_id=supplier_id,
        date_filter=date_filter
    )


async def get_direct_purchase_by_id(
    request: Request,
    response: Response,
    purchase_id: UUID
) -> Dict[str, Any]:
    """
    Get a direct purchase by ID with all details
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Get purchase
            purchase = await conn.fetchrow("""
                SELECT
                    tp.*,
                    ts.name as supplier_name,
                    ts.tax_id as supplier_tax_id,
                    ts.phone as supplier_phone,
                    ts.email as supplier_email
                FROM tenant_purchases tp
                LEFT JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                WHERE tp.id = $1 AND tp.tenant_id = $2 AND tp.is_direct_entry = TRUE
            """, purchase_id, tenant_id)

            if not purchase:
                raise HTTPException(status_code=404, detail="Compra directa no encontrada")

            purchase_dict = dict(purchase)

            # Get items with ingredient details
            items = await conn.fetch("""
                SELECT
                    tpi.*,
                    i.name as ingredient_name,
                    i.category as ingredient_category
                FROM tenant_purchase_items tpi
                JOIN ingredients i ON tpi.ingredient_id = i.id
                WHERE tpi.purchase_id = $1
                ORDER BY i.name
            """, purchase_id)

            purchase_dict['items'] = [dict(item) for item in items]

            # Get status history
            history = await conn.fetch("""
                SELECT
                    psh.*,
                    p.name as changed_by_name
                FROM purchase_status_history psh
                LEFT JOIN profile p ON psh.changed_by = p.id
                WHERE psh.purchase_id = $1
                ORDER BY psh.changed_at DESC
            """, purchase_id)

            purchase_dict['status_history'] = [dict(h) for h in history]

            # Get attachments
            attachments = await conn.fetch("""
                SELECT * FROM purchase_attachments
                WHERE purchase_id = $1
                ORDER BY uploaded_at DESC
            """, purchase_id)

            purchase_dict['attachments'] = [dict(a) for a in attachments]

            return {
                "success": True,
                "data": purchase_dict
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_direct_purchase_by_id: {str(e)}")
        logger.exception(e)
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def get_supplier_catalog_prices(
    request: Request,
    response: Response,
    supplier_id: UUID
) -> Dict[str, Any]:
    """
    Get catalog prices for a specific supplier.
    Returns ingredients with their configured prices for this supplier.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Get all active ingredients with their purchase units
            ingredients_data = await conn.fetch("""
                SELECT
                    i.id as ingredient_id,
                    i.name as ingredient_name,
                    i.unit as base_unit,
                    i.category,
                    i.type,
                    i.costo_unitario as default_price
                FROM ingredients i
                WHERE i.tenant_id = $1
                ORDER BY i.name
            """, tenant_id)

            # Get all purchase units for ingredients
            purchase_units = await conn.fetch("""
                SELECT
                    ipu.ingredient_id,
                    ipu.purchase_unit_label,
                    ipu.conversion_factor,
                    ipu.unit_cost,
                    ipu.is_default
                FROM ingredient_purchase_units ipu
                JOIN ingredients i ON ipu.ingredient_id = i.id
                WHERE i.tenant_id = $1 AND ipu.is_active = TRUE
                ORDER BY ipu.is_default DESC, ipu.purchase_unit_label
            """, tenant_id)

            # Group purchase units by ingredient
            units_by_ingredient = {}
            for unit in purchase_units:
                ing_id = str(unit['ingredient_id'])
                if ing_id not in units_by_ingredient:
                    units_by_ingredient[ing_id] = []
                units_by_ingredient[ing_id].append({
                    "label": unit['purchase_unit_label'],
                    "conversion_factor": float(unit['conversion_factor']) if unit['conversion_factor'] else 1,
                    "unit_cost": float(unit['unit_cost']) if unit['unit_cost'] else None,
                    "is_default": unit['is_default']
                })

            # Build catalog
            catalog = []
            for row in ingredients_data:
                ing_id = str(row['ingredient_id'])
                units = units_by_ingredient.get(ing_id, [{
                    "label": row['base_unit'],
                    "conversion_factor": 1,
                    "unit_cost": float(row['default_price']) if row['default_price'] else None,
                    "is_default": True
                }])

                catalog.append({
                    "ingredient_id": ing_id,
                    "ingredient_name": row['ingredient_name'],
                    "base_unit": row['base_unit'],
                    "category": row['category'],
                    "type": row['type'],
                    "default_price": float(row['default_price']) if row['default_price'] else None,
                    "purchase_units": units
                })

            return {
                "success": True,
                "data": catalog,
                "total": len(catalog)
            }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting supplier catalog: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def update_direct_purchase(
    request: Request,
    response: Response,
    purchase_id: UUID,
    items_data: str,
    purchase_date: Optional[str] = None,
    notes: Optional[str] = None,
    invoice_number: Optional[str] = None,
    payment_type: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    payment_reference: Optional[str] = None,
    payment_amount: Optional[float] = None,
    payment_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update a direct purchase.

    This function:
    1. Updates purchase metadata (notes, invoice_number, payment info)
    2. Updates items - adjusts inventory for changes
    3. Uploads new attachments (does not delete existing ones)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse items data from JSON string
        try:
            items = json.loads(items_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid items data format")

        if not items or len(items) == 0:
            raise HTTPException(status_code=400, detail="At least one item is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Get existing purchase and verify ownership
                existing_purchase = await conn.fetchrow("""
                    SELECT id, status, purchase_number, tenant_id, payment_type
                    FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2 AND is_direct_entry = TRUE
                """, purchase_id, tenant_id)

                if not existing_purchase:
                    raise HTTPException(status_code=404, detail="Compra directa no encontrada")

                purchase_number = existing_purchase['purchase_number']
                effective_payment_type = (
                    payment_type
                    if payment_type is not None
                    else existing_purchase["payment_type"]
                )
                assert_contado_requires_payment_method(effective_payment_type, payment_method)

                # 2. Get existing items WITH ingredient names for audit trail
                existing_items = await conn.fetch("""
                    SELECT
                        pi.id,
                        pi.ingredient_id,
                        pi.quantity,
                        pi.unit,
                        pi.purchase_quantity,
                        pi.purchase_unit,
                        pi.unit_cost,
                        pi.total_cost,
                        i.name as ingredient_name
                    FROM tenant_purchase_items pi
                    JOIN ingredients i ON pi.ingredient_id = i.id
                    WHERE pi.purchase_id = $1
                """, purchase_id)

                # Store items BEFORE edit for audit trail
                items_before_list = [dict(row) for row in existing_items]
                total_before = sum(
                    (_direct_purchase_decimal(item.get('total_cost')) for item in items_before_list),
                    _DECIMAL_ZERO,
                )

                # ... (rest of the update logic would go here)
                # Since we are adding a NEW function, I will perform a pure append/insert using read_file context.
                # Actually, I used replace with context, but I should probably just append it at the end of the file or after update_direct_purchase.
                # But wait, I'm inside a tool call that requires me to define everything.
                # The Plan:
                # 1. READ the end of update_direct_purchase function to see where it ends.
                # 2. Append the new function.

# Let's verify where update_direct_purchase ends. Use view_file first.

                # 3. Reverse inventory for existing items
                for old_item in existing_items:
                    ingredient_id = old_item['ingredient_id']
                    old_quantity = _direct_purchase_decimal(old_item['quantity'])

                    # Get current inventory
                    inventory_row = await conn.fetchrow("""
                        SELECT id, current_stock
                        FROM tenant_inventory
                        WHERE tenant_id = $1 AND ingredient_id = $2
                        FOR UPDATE
                    """, tenant_id, ingredient_id)

                    if inventory_row:
                        current_stock = _direct_purchase_decimal(inventory_row['current_stock'])
                        new_stock = current_stock - old_quantity

                        await conn.execute("""
                            UPDATE tenant_inventory
                            SET current_stock = $1, last_updated = NOW()
                            WHERE tenant_id = $2 AND ingredient_id = $3
                        """, max(_DECIMAL_ZERO, new_stock), tenant_id, ingredient_id)

                        # Record inventory movement (reversal)
                        await conn.execute("""
                            INSERT INTO tenant_ingredient_movements (
                                tenant_id,
                                ingredient_id,
                                movement_type,
                                quantity_change,
                                unit,
                                previous_stock,
                                new_stock,
                                reference_table,
                                reference_id,
                                notes,
                                created_by
                            ) VALUES ($1, $2, 'adjustment', $3, $4, $5, $6, 'tenant_purchases', $7, $8, $9)
                        """,
                            tenant_id,
                            ingredient_id,
                            -old_quantity,
                            old_item['unit'],
                            current_stock,
                            max(_DECIMAL_ZERO, new_stock),
                            purchase_id,
                            f"Ajuste por edición de compra directa - {purchase_number}",
                            user_id
                        )

                # 4. Delete existing items
                await conn.execute("""
                    DELETE FROM tenant_purchase_items WHERE purchase_id = $1
                """, purchase_id)

                # 5. Insert new items and update inventory
                total_amount = _DECIMAL_ZERO
                for item in items:
                    ingredient_id_str = item.get('ingredient_id')

                    try:
                        ingredient_id = UUID(ingredient_id_str) if isinstance(ingredient_id_str, str) else ingredient_id_str
                    except (ValueError, TypeError):
                        logger.error(f"Invalid ingredient_id format: {ingredient_id_str}")
                        continue

                    # Get ingredient base unit and weight info for catalog conversions
                    ingredient = await conn.fetchrow("""
                        SELECT unit, unit_weight_gr, unit_weight_unit FROM ingredients WHERE id = $1
                    """, ingredient_id)

                    if not ingredient:
                        logger.warning(f"Ingredient not found: {ingredient_id}")
                        continue

                    base_unit = ingredient['unit']
                    purchase_unit = item.get('purchase_unit', base_unit)

                    # Get conversion factor
                    conversion_factor = _DECIMAL_ONE
                    if purchase_unit and purchase_unit != base_unit:
                        conversion_row = await conn.fetchrow("""
                            SELECT conversion_factor
                            FROM ingredient_purchase_units
                            WHERE ingredient_id = $1
                              AND purchase_unit_label = $2
                              AND is_active = TRUE
                        """, ingredient_id, purchase_unit)

                        if conversion_row:
                            conversion_factor = _direct_purchase_decimal(conversion_row['conversion_factor'])
                        else:
                            # Fallback: catalog unit (lt, kg, galon…) — two-step conversion for und ingredients
                            catalog_factor = _catalog_direct_purchase_conversion_factor(
                                purchase_unit,
                                base_unit,
                                ingredient,
                            )
                            if catalog_factor is not None:
                                conversion_factor = catalog_factor

                    values = _resolve_direct_purchase_item_decimals(
                        item,
                        base_unit,
                        purchase_unit,
                        conversion_factor,
                    )
                    purchase_quantity = values["purchase_quantity"]
                    conversion_factor = values["conversion_factor"]
                    base_quantity = values["base_quantity"]
                    base_unit_cost = values["base_unit_cost"]
                    item_total = values["item_total"]
                    total_amount += item_total

                    # Insert purchase item
                    await conn.execute("""
                        INSERT INTO tenant_purchase_items (
                            purchase_id,
                            ingredient_id,
                            quantity,
                            unit,
                            purchase_quantity,
                            purchase_unit,
                            unit_cost,
                            total_cost,
                            quantity_received,
                            received_at,
                            quality_status,
                            item_condition,
                            notes
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), 'good', 'complete', $10)
                    """,
                        purchase_id,
                        ingredient_id,
                        base_quantity,
                        base_unit,
                        purchase_quantity,
                        purchase_unit,
                        base_unit_cost,
                        item_total,
                        base_quantity,
                        item.get('notes')
                    )

                    # Update inventory
                    inventory_row = await conn.fetchrow("""
                        SELECT id, current_stock
                        FROM tenant_inventory
                        WHERE tenant_id = $1 AND ingredient_id = $2
                        FOR UPDATE
                    """, tenant_id, ingredient_id)

                    previous_stock = _DECIMAL_ZERO
                    if inventory_row:
                        previous_stock = _direct_purchase_decimal(inventory_row['current_stock'])
                        new_stock = previous_stock + base_quantity

                        await conn.execute("""
                            UPDATE tenant_inventory
                            SET current_stock = $1, last_updated = NOW()
                            WHERE tenant_id = $2 AND ingredient_id = $3
                        """, new_stock, tenant_id, ingredient_id)
                    else:
                        new_stock = base_quantity
                        await conn.execute("""
                            INSERT INTO tenant_inventory (
                                tenant_id, ingredient_id, current_stock, minimum_stock
                            ) VALUES ($1, $2, $3, 0)
                        """, tenant_id, ingredient_id, new_stock)

                    # Create inventory movement record
                    await conn.execute("""
                        INSERT INTO tenant_ingredient_movements (
                            tenant_id,
                            ingredient_id,
                            movement_type,
                            quantity_change,
                            unit,
                            previous_stock,
                            new_stock,
                            reference_table,
                            reference_id,
                            cost_per_unit,
                            notes,
                            created_by
                        ) VALUES ($1, $2, 'purchase', $3, $4, $5, $6, 'tenant_purchases', $7, $8, $9, $10)
                    """,
                        tenant_id,
                        ingredient_id,
                        base_quantity,
                        base_unit,
                        previous_stock,
                        new_stock,
                        purchase_id,
                        base_unit_cost,
                        f"Compra directa (editada) - {purchase_number}",
                        user_id
                    )

                # 6. Get items AFTER edit for audit trail
                items_after_rows = await conn.fetch("""
                    SELECT
                        pi.id,
                        pi.ingredient_id,
                        pi.quantity,
                        pi.unit,
                        pi.purchase_quantity,
                        pi.purchase_unit,
                        pi.unit_cost,
                        pi.total_cost,
                        i.name as ingredient_name
                    FROM tenant_purchase_items pi
                    JOIN ingredients i ON pi.ingredient_id = i.id
                    WHERE pi.purchase_id = $1
                """, purchase_id)

                items_after_list = [dict(row) for row in items_after_rows]
                total_after = total_amount

                # 7. Calculate changes summary and create audit trail
                changes_summary = calculate_changes_summary(items_before_list, items_after_list)

                # Create history entry if there were any changes
                if changes_summary['added'] or changes_summary['removed'] or changes_summary['modified']:
                    # Serialize items for JSON storage
                    def serialize_item(item):
                        return {
                            'ingredient_id': str(item['ingredient_id']),
                            'ingredient_name': item.get('ingredient_name', 'N/A'),
                            'purchase_quantity': float(item.get('purchase_quantity') or item.get('quantity', 0)),
                            'purchase_unit': item.get('purchase_unit') or item.get('unit', ''),
                            'unit_cost': float(item.get('unit_cost', 0)),
                            'total_cost': float(item.get('total_cost', 0))
                        }

                    audit_metadata = {
                        "action": "items_edited",
                        "items_before": [serialize_item(i) for i in items_before_list],
                        "items_after": [serialize_item(i) for i in items_after_list],
                        "changes_summary": changes_summary,
                        "totals": {
                            "before": float(total_before),
                            "after": float(total_after),
                            "difference": float(total_after - total_before)
                        }
                    }

                    await conn.execute("""
                        INSERT INTO purchase_status_history (
                            id, purchase_id, tenant_id, from_status, to_status,
                            changed_by, changed_at, metadata, notes
                        ) VALUES (
                            $1, $2, $3, 'received', 'received',
                            $4, NOW(), $5, 'Items editados'
                        )
                    """,
                        uuid4(),
                        purchase_id,
                        tenant_id,
                        user_id,
                        json.dumps(audit_metadata)
                    )

                    logger.info(f"Created audit trail for direct purchase {purchase_number}: {len(changes_summary['added'])} added, {len(changes_summary['removed'])} removed, {len(changes_summary['modified'])} modified")

                # 8. Determine new status
                current_status = existing_purchase['status']
                new_status = current_status
                if payment_method and payment_amount:
                    new_status = 'paid'
                elif invoice_number:
                    new_status = 'invoiced'

                payment_method, payment_method_id = await _normalize_direct_purchase_payment(
                    conn,
                    payment_method,
                    payment_method_id,
                )
                assert_contado_requires_payment_method(effective_payment_type, payment_method)

                # 9. Update purchase record
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        total_amount = $1,
                        notes = $2,
                        invoice_number = $3,
                        payment_type = COALESCE($13, payment_type),
                        payment_method = $4,
                        payment_method_id = $5::uuid,
                        payment_reference = $6,
                        payment_amount = $7,
                        payment_date = $8,
                        status = $9,
                        updated_at = NOW(),
                        paid_at = CASE WHEN $10 THEN NOW() ELSE paid_at END,
                        purchase_date = COALESCE($12, purchase_date)
                    WHERE id = $11
                """,
                    total_amount,
                    notes,
                    invoice_number,
                    payment_method,
                    payment_method_id,
                    payment_reference,
                    payment_amount,
                    _parse_date(payment_date),
                    new_status,
                    bool(payment_method and payment_amount and current_status != 'paid'),
                    purchase_id,
                    _parse_date(purchase_date),
                    payment_type,
                )

                # 10. Create status history if status changed
                if new_status != current_status:
                    await create_status_history_entry(
                        conn, purchase_id, tenant_id,
                        current_status, new_status, user_id,
                        {"updated_via": "edit"},
                        f"Estado actualizado durante edición"
                    )

                # 11. Attachments are now uploaded via separate endpoint
                # POST /suppliers/purchases/{purchase_id}/attachments

                return {
                    "success": True,
                    "message": "Compra directa actualizada exitosamente",
                    "data": {
                        "id": str(purchase_id),
                        "purchase_number": purchase_number,
                        "status": new_status,
                        "total_amount": total_amount,
                        "items_count": len(items),
                        "inventory_updated": True
                    }
                }

    except Exception as e:
        logger.error(f"Error in update_direct_purchase: {str(e)}")
        logger.exception(e)
        raise HTTPException(
            status_code=500,
            detail=f"Error actualizando compra directa: {str(e)}"
        )


async def upload_direct_purchase_attachments(
    request: Request,
    response: Response,
    purchase_id: UUID,
    invoice_files: List[UploadFile] = [],
    payment_files: List[UploadFile] = []
) -> Dict[str, Any]:
    """
    Upload attachments for a direct purchase.
    Handles multiple file types in a single request.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not invoice_files and not payment_files:
            return {"success": True, "message": "No files to upload"}

        async with get_db_connection() as conn:
            # Verify purchase exists and belongs to tenant
            purchase = await conn.fetchrow("""
                SELECT id, status FROM tenant_purchases
                WHERE id = $1 AND tenant_id = $2 AND is_direct_entry = TRUE
            """, purchase_id, tenant_id)

            if not purchase:
                raise HTTPException(status_code=404, detail="Compra directa no encontrada")

            # Upload invoice files
            if invoice_files:
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=invoice_files,
                    attachment_type='invoice',
                    description_prefix='Factura compra directa',
                    related_status=purchase['status'],
                    log_prefix='UPLOAD-INVOICE'
                )

            # Upload payment files
            if payment_files:
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=payment_files,
                    attachment_type='payment_proof',
                    description_prefix='Soporte pago compra directa',
                    related_status=purchase['status'],
                    log_prefix='UPLOAD-PAYMENT'
                )

            return {
                "success": True,
                "message": "Archivos subidos exitosamente",
                "data": {
                    "purchase_id": str(purchase_id),
                    "invoice_files_count": len(invoice_files),
                    "payment_files_count": len(payment_files)
                }
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in upload_direct_purchase_attachments: {str(e)}")
        logger.exception(e)
        raise HTTPException(status_code=500, detail="Error subiendo archivos")
