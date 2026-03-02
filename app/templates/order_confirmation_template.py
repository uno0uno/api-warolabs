"""
Order confirmation email template.
Sent to the customer immediately after an online order is placed (status: pending).
Plain text format — matches the style of other transactional emails in the system.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
from app.config import settings

BOGOTA_TZ = ZoneInfo("America/Bogota")

_DAYS_ES = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"
]
_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def format_bogota_datetime(dt: datetime) -> str:
    """Convert a UTC-aware datetime to a Spanish Colombia-formatted string."""
    local = dt.astimezone(BOGOTA_TZ)
    weekday = _DAYS_ES[local.weekday()]
    month = _MONTHS_ES[local.month - 1]
    hour = local.hour % 12 or 12
    ampm = "a. m." if local.hour < 12 else "p. m."
    return f"{weekday}, {local.day} de {month} de {local.year}, {hour}:{local.minute:02d} {ampm}"


def _format_cop(amount: float) -> str:
    """Format a number as Colombian Peso (no decimals)."""
    return f"${amount:,.0f}".replace(",", ".")


def _order_type_label(order_type: str) -> str:
    return {
        "delivery": "Domicilio",
        "pickup": "Recoger en tienda",
        "dine-in": "En mesa",
    }.get(order_type, order_type)


def get_order_confirmation_text(
    order_number: int,
    order_type: str,
    order_date: datetime,
    items: list,
    subtotal: float,
    delivery_address: Optional[dict],
    scheduled_time: Optional[datetime],
    delivery_instructions: Optional[str],
    pickup_pin: Optional[str],
    order_id: Optional[str] = None,
) -> str:
    """
    Build the plain text body for an order confirmation email.

    Args:
        order_number: The sequential order number.
        order_type: 'delivery' | 'pickup' | 'dine-in'
        order_date: UTC-aware datetime of order creation.
        items: List of cart item dicts (product_name, quantity, unit_price, subtotal, modifiers).
        subtotal: Cart subtotal in COP (does not include delivery fee).
        delivery_address: Dict with address fields, or None.
        scheduled_time: UTC-aware datetime if order is scheduled, or None.
        delivery_instructions: Free-text instructions, or None.
        pickup_pin: 6-digit PIN if order_type is 'pickup', or None.
    """
    delivery_fee = 5000.0 if order_type == "delivery" and subtotal < 50000 else 0.0
    total = subtotal + delivery_fee

    order_date_str = format_bogota_datetime(order_date)
    type_label = _order_type_label(order_type)

    # Items block
    items_lines = []
    for item in items:
        qty = item.get("quantity", 1)
        name = item.get("product_name", "Producto")
        item_subtotal = _format_cop(float(item.get("subtotal", 0)))
        line = f"  {qty}x {name} — {item_subtotal}"
        if item.get("modifiers"):
            mod_names = ", ".join(
                m.get("modifier_name") or m.get("name", "") for m in item["modifiers"]
            )
            line += f"\n     + {mod_names}"
        items_lines.append(line)
    items_block = "\n".join(items_lines)

    # Totals block
    totals_lines = [f"Subtotal: {_format_cop(subtotal)}"]
    if delivery_fee > 0:
        totals_lines.append(f"Domicilio: {_format_cop(delivery_fee)}")
    totals_lines.append(f"Total: {_format_cop(total)}")
    totals_block = "\n".join(totals_lines)

    # Optional sections
    pickup_pin_block = ""
    if pickup_pin:
        pickup_pin_block = f"\nTU PIN DE RECOGIDA\n------------------\n{pickup_pin}\nMuestra este PIN al recoger tu pedido.\n"

    address_block = ""
    if order_type == "delivery" and delivery_address:
        line1 = delivery_address.get("address_line1", "")
        line2 = delivery_address.get("address_line2", "")
        city = delivery_address.get("city", "")
        state = delivery_address.get("state", "")
        notes = delivery_address.get("delivery_notes", "")
        addr = line1
        if line2:
            addr += f", {line2}"
        addr += f", {city}, {state}"
        if notes:
            addr += f"\nNotas: {notes}"
        address_block = f"\nDirección de entrega: {addr}\n"

    scheduled_block = ""
    if scheduled_time:
        scheduled_str = format_bogota_datetime(scheduled_time)
        scheduled_block = f"\nHora programada: {scheduled_str}\n"

    instructions_block = ""
    if delivery_instructions:
        instructions_block = f"\nInstrucciones: {delivery_instructions}\n"

    tracking_block = ""
    if order_id:
        tracking_block = f"\nVER TU PEDIDO\n-------------\nPuedes seguir el estado de tu pedido en:\n{settings.frontend_url}/mis-pedidos/{order_id}\n"

    return f"""¡Hola!

Tu pedido ha sido recibido y está siendo revisado por el restaurante.

RESUMEN DEL PEDIDO
------------------
Pedido: #{order_number}
Tipo: {type_label}
Fecha: {order_date_str}
{pickup_pin_block}{address_block}{scheduled_block}{instructions_block}
PRODUCTOS
---------
{items_block}

TOTAL
-----
{totals_block}

Pago: Efectivo contra entrega

El restaurante está revisando tu pedido. Te notificaremos cuando esté confirmado.
{tracking_block}
Gracias,
WARO Colombia
{settings.email_from or "hola@warocol.com"}
""".strip()


def get_order_confirmation_subject(order_number: int) -> str:
    """Email subject line for order confirmation."""
    return f"Tu pedido #{order_number} está siendo confirmado — WARO"


def get_order_accepted_text(
    order_number: int,
    order_type: str,
    items: list,
    subtotal: float,
    delivery_address: Optional[dict],
    order_id: Optional[str] = None,
) -> str:
    """
    Build the plain text body for an order acceptance email.

    Sent when the restaurant confirms the order via the auto_complete path
    (pending → completed). Simpler than the creation email — no pickup PIN,
    no scheduled time, no delivery instructions.
    """
    type_label = _order_type_label(order_type)

    items_lines = []
    for item in items:
        qty = item.get("quantity", 1)
        name = item.get("product_name", "Producto")
        item_subtotal = _format_cop(float(item.get("subtotal", 0)))
        line = f"  {qty}x {name} — {item_subtotal}"
        if item.get("modifiers"):
            mod_names = ", ".join(
                m.get("modifier_name") or m.get("name", "") for m in item["modifiers"]
            )
            line += f"\n     + {mod_names}"
        items_lines.append(line)
    items_block = "\n".join(items_lines)

    total_block = f"Total: {_format_cop(subtotal)}"

    address_block = ""
    if order_type == "delivery" and delivery_address:
        line1 = delivery_address.get("address_line1", "")
        line2 = delivery_address.get("address_line2", "")
        city = delivery_address.get("city", "")
        notes = delivery_address.get("delivery_notes", "")
        addr = line1
        if line2:
            addr += f", {line2}"
        if city:
            addr += f", {city}"
        if notes:
            addr += f"\nNotas: {notes}"
        address_block = f"\nDirección de entrega: {addr}\n"

    tracking_block = ""
    if order_id:
        tracking_block = f"\nVER TU PEDIDO\n-------------\nPuedes seguir el estado de tu pedido en:\n{settings.frontend_url}/mis-pedidos/{order_id}\n"

    return f"""¡Buenas noticias!

Tu pedido #{order_number} fue aceptado — el restaurante ya lo está preparando.

RESUMEN DEL PEDIDO
------------------
Pedido: #{order_number}
Tipo: {type_label}
{address_block}
PRODUCTOS
---------
{items_block}

TOTAL
-----
{total_block}

Pago: Efectivo contra entrega
{tracking_block}
Gracias,
WARO Colombia
{settings.email_from or "hola@warocol.com"}
""".strip()


def get_order_accepted_subject(order_number: int) -> str:
    """Email subject line for order acceptance notification."""
    return f"¡Tu pedido #{order_number} fue aceptado! — WARO"
