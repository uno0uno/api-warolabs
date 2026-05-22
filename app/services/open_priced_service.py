"""Open-priced (venta libre) line price validation for POS tab and cart writes."""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.exceptions import APIError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

OPEN_SALE_DEFAULT_NAME = "Venta libre"
OPEN_SALE_CATALOG_PRICE = Decimal("1")

_PRICE_TOLERANCE = Decimal("0.01")


def _to_money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _prices_match(catalog: Decimal, requested: Decimal) -> bool:
    return abs(catalog - requested) <= _PRICE_TOLERANCE


async def fetch_product_pricing_map(
    conn,
    tenant_id: UUID,
    product_ids: List[UUID],
) -> Dict[str, Dict[str, Any]]:
    """Prefetch catalog price + open_priced flag for a set of products."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT id, price, open_priced
        FROM product
        WHERE tenant_id = $1 AND id = ANY($2::uuid[])
        """,
        tenant_id,
        product_ids,
    )
    return {
        str(row["id"]): {
            "price": _to_money(row["price"]),
            "open_priced": bool(row["open_priced"]),
        }
        for row in rows
    }


def resolve_line_unit_price(
    pricing: Optional[Dict[str, Any]],
    product_id: UUID,
    requested_unit_price: Any,
    modifiers: Optional[List] = None,
) -> Decimal:
    """
    Return the unit price to persist for a POS line.

    - Normal products: requested must match catalog (within tolerance).
    - Open-priced: any positive price; modifiers not allowed (MVP).
    """
    key = str(product_id)
    if pricing is None or key not in pricing:
        raise NotFoundError(f"Product {product_id} not found")

    row = pricing[key]
    catalog = row["price"]
    open_priced = row["open_priced"]
    mods = modifiers or []

    if open_priced:
        if mods:
            raise ValidationError(
                "Open-priced items cannot include modifiers",
                details={"product_id": key},
            )
        resolved = _to_money(requested_unit_price)
        if resolved <= 0:
            raise ValidationError(
                "Open-priced unit price must be greater than zero",
                details={"product_id": key},
            )
        return resolved

    if mods:
        # Modifiers allowed; unit_price is base only — still must match catalog.
        pass

    resolved = _to_money(requested_unit_price)
    if not _prices_match(catalog, resolved):
        raise ValidationError(
            "Unit price does not match catalog price for this product",
            details={
                "product_id": key,
                "catalog_price": float(catalog),
                "requested_price": float(resolved),
            },
        )
    return catalog


def validate_items_unit_prices(
    pricing_map: Dict[str, Dict[str, Any]],
    items: List[dict],
) -> None:
    """Validate and normalize unit_price on each item dict in place."""
    for item in items:
        product_id = item["product_id"]
        resolved = resolve_line_unit_price(
            pricing_map,
            product_id,
            item["unit_price"],
            item.get("modifiers"),
        )
        item["unit_price"] = float(resolved)


async def assert_single_open_priced_per_tenant(
    conn,
    tenant_id: UUID,
    *,
    exclude_product_id: Optional[UUID] = None,
) -> None:
    """Ensure at most one open_priced product exists for the tenant."""
    row = await conn.fetchrow(
        """
        SELECT id, name
        FROM product
        WHERE tenant_id = $1
          AND open_priced = true
          AND ($2::uuid IS NULL OR id != $2)
        LIMIT 1
        """,
        tenant_id,
        exclude_product_id,
    )
    if row:
        raise APIError(
            f"Tenant already has an open-priced product: {row['name']}",
            status_code=409,
            details={"existing_product_id": str(row["id"])},
        )


async def fetch_open_sale_product(
    conn,
    tenant_id: UUID,
) -> Optional[Dict[str, str]]:
    """Return {id, name} for the tenant's open-priced shell product, if exactly one."""
    rows = await conn.fetch(
        """
        SELECT id, name
        FROM product
        WHERE tenant_id = $1
          AND open_priced = true
          AND is_available = true
        ORDER BY name
        """,
        tenant_id,
    )
    if len(rows) == 1:
        return {"id": str(rows[0]["id"]), "name": rows[0]["name"]}
    if len(rows) > 1:
        logger.warning(
            "Tenant %s has %s open_priced products; open_sale_product omitted",
            tenant_id,
            len(rows),
        )
    return None


async def ensure_open_sale_product(conn, tenant_id: UUID) -> Dict[str, str]:
    """Create or reactivate the tenant shell product for venta libre (#805)."""
    existing = await conn.fetchrow(
        """
        SELECT id, name
        FROM product
        WHERE tenant_id = $1 AND open_priced = true
        LIMIT 1
        """,
        tenant_id,
    )
    if existing:
        await conn.execute(
            """
            UPDATE product
            SET is_available = true,
                allow_modifiers = false,
                open_priced = true,
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            existing["id"],
            tenant_id,
        )
        return {"id": str(existing["id"]), "name": existing["name"]}

    category = await conn.fetchrow(
        """
        SELECT id FROM categories
        WHERE tenant_id = $1
        ORDER BY created_at ASC
        LIMIT 1
        """,
        tenant_id,
    )
    if not category:
        raise APIError(
            "Crea al menos una categoría en el menú antes de activar venta libre en el POS.",
            status_code=409,
        )

    row = await conn.fetchrow(
        """
        INSERT INTO product (
            name, description, price, category_id, product_base_type_id, preparation_time,
            controla_stock, is_available, is_available_online, is_available_table_qr,
            is_combo, is_resale, open_priced, allow_modifiers,
            tax_category, tenant_id, station_id, kitchen_name, image_url, costo_percibido
        )
        VALUES (
            $1, NULL, $2, $3, NULL, NULL,
            true, true, false, false,
            false, false, true, false,
            'standard', $4, NULL, NULL, NULL, NULL
        )
        RETURNING id, name
        """,
        OPEN_SALE_DEFAULT_NAME,
        OPEN_SALE_CATALOG_PRICE,
        category["id"],
        tenant_id,
    )
    return {"id": str(row["id"]), "name": row["name"]}


async def deactivate_open_sale_product(conn, tenant_id: UUID) -> None:
    """Hide the shell product from POS without dropping open_priced (#805)."""
    await conn.execute(
        """
        UPDATE product
        SET is_available = false, updated_at = now()
        WHERE tenant_id = $1 AND open_priced = true
        """,
        tenant_id,
    )
