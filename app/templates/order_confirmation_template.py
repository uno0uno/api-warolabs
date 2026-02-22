"""
Order confirmation email template.
Sent to the customer immediately after an online order is placed (status: pending).
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

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
    ampm = "a.\u00a0m." if local.hour < 12 else "p.\u00a0m."
    return f"{weekday}, {local.day} de {month} de {local.year}, {hour}:{local.minute:02d}\u00a0{ampm}"


def _format_cop(amount: float) -> str:
    """Format a number as Colombian Peso (no decimals)."""
    return f"${amount:,.0f}".replace(",", ".")


def _order_type_label(order_type: str) -> str:
    return {
        "delivery": "Domicilio",
        "pickup": "Recoger en tienda",
        "dine-in": "En mesa",
    }.get(order_type, order_type)


def _build_items_rows(items: list) -> str:
    rows = ""
    for item in items:
        mods = ""
        if item.get("modifiers"):
            mod_names = ", ".join(m.get("modifier_name") or m.get("name", "") for m in item["modifiers"])
            mods = f'<div style="font-size:12px;color:#666;margin-top:2px;">+ {mod_names}</div>'

        unit_price = _format_cop(float(item.get("unit_price", 0)))
        subtotal = _format_cop(float(item.get("subtotal", 0)))
        qty = item.get("quantity", 1)
        name = item.get("product_name", "Producto")

        rows += f"""
        <tr>
          <td style="padding:8px 0;border-bottom:1px solid #f0f0f0;vertical-align:top;">
            <span style="font-weight:600;">{qty}×</span> {name}
            {mods}
          </td>
          <td style="padding:8px 0;border-bottom:1px solid #f0f0f0;text-align:right;vertical-align:top;white-space:nowrap;">
            {unit_price}
          </td>
          <td style="padding:8px 8px 8px 16px;border-bottom:1px solid #f0f0f0;text-align:right;vertical-align:top;white-space:nowrap;font-weight:600;">
            {subtotal}
          </td>
        </tr>"""
    return rows


def get_order_confirmation_html(
    order_number: int,
    order_type: str,
    order_date: datetime,
    items: list,
    subtotal: float,
    delivery_address: Optional[dict],
    scheduled_time: Optional[datetime],
    delivery_instructions: Optional[str],
    pickup_pin: Optional[str],
) -> str:
    """
    Build the HTML body for an order confirmation email.

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
    items_rows = _build_items_rows(items)

    # --- Optional sections ---

    # Delivery address block
    address_block = ""
    if order_type == "delivery" and delivery_address:
        line1 = delivery_address.get("address_line1", "")
        line2 = delivery_address.get("address_line2", "")
        city = delivery_address.get("city", "")
        state = delivery_address.get("state", "")
        notes = delivery_address.get("delivery_notes", "")

        addr_lines = f"{line1}"
        if line2:
            addr_lines += f", {line2}"
        addr_lines += f"<br>{city}, {state}"
        if notes:
            addr_lines += f'<br><em style="color:#666;">{notes}</em>'

        address_block = f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f0f0f0;">
            <span style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:.5px;">Dirección de entrega</span><br>
            <span style="font-size:14px;">{addr_lines}</span>
          </td>
        </tr>"""

    # Scheduled time block
    scheduled_block = ""
    if scheduled_time:
        scheduled_str = format_bogota_datetime(scheduled_time)
        scheduled_block = f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f0f0f0;">
            <span style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:.5px;">Hora programada</span><br>
            <span style="font-size:14px;">{scheduled_str}</span>
          </td>
        </tr>"""

    # Instructions block
    instructions_block = ""
    if delivery_instructions:
        instructions_block = f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f0f0f0;">
            <span style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:.5px;">Instrucciones adicionales</span><br>
            <span style="font-size:14px;font-style:italic;">{delivery_instructions}</span>
          </td>
        </tr>"""

    # Pickup PIN block
    pin_block = ""
    if pickup_pin:
        pin_block = f"""
        <div style="background:#fffbeb;border:2px solid #fbbf24;border-radius:12px;padding:20px;text-align:center;margin:20px 0;">
          <p style="margin:0 0 6px 0;font-size:11px;font-weight:700;color:#92400e;text-transform:uppercase;letter-spacing:1px;">Tu PIN de recogida</p>
          <p style="margin:0 0 6px 0;font-size:36px;font-weight:900;color:#78350f;letter-spacing:.2em;">{pickup_pin}</p>
          <p style="margin:0;font-size:12px;color:#b45309;">Muestra este PIN al recoger tu pedido</p>
        </div>"""

    # Delivery fee row
    delivery_fee_row = ""
    if delivery_fee > 0:
        delivery_fee_row = f"""
        <tr>
          <td colspan="2" style="padding:6px 0;text-align:right;color:#666;font-size:14px;">Domicilio</td>
          <td style="padding:6px 8px;text-align:right;font-size:14px;">{_format_cop(delivery_fee)}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pedido #{order_number} — WARO</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;color:#1a1a1a;">
  <div style="max-width:600px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e5e5e5;">

    <!-- Header -->
    <div style="background:#1a1a1a;padding:24px 32px;">
      <p style="margin:0;font-size:22px;font-weight:700;color:#fff;letter-spacing:.5px;">WARO</p>
      <p style="margin:4px 0 0 0;font-size:13px;color:#999;">Colombia</p>
    </div>

    <!-- Body -->
    <div style="padding:32px;">

      <h1 style="margin:0 0 4px 0;font-size:22px;font-weight:700;">¡Pedido recibido!</h1>
      <p style="margin:0 0 24px 0;color:#555;font-size:15px;">
        Pedido <strong>#{ order_number }</strong> · {type_label} · {order_date_str}
      </p>

      {pin_block}

      <!-- Order meta -->
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        {address_block}
        {scheduled_block}
        {instructions_block}
      </table>

      <!-- Items -->
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <thead>
          <tr>
            <th style="text-align:left;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;padding-bottom:8px;border-bottom:2px solid #e5e5e5;">Producto</th>
            <th style="text-align:right;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;padding-bottom:8px;border-bottom:2px solid #e5e5e5;">Precio</th>
            <th style="text-align:right;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;padding-bottom:8px;border-bottom:2px solid #e5e5e5;padding-left:16px;">Total</th>
          </tr>
        </thead>
        <tbody>
          {items_rows}
        </tbody>
      </table>

      <!-- Totals -->
      <table style="width:100%;border-collapse:collapse;margin-bottom:28px;">
        <tr>
          <td colspan="2" style="padding:6px 0;text-align:right;color:#666;font-size:14px;">Subtotal</td>
          <td style="padding:6px 8px;text-align:right;font-size:14px;">{_format_cop(subtotal)}</td>
        </tr>
        {delivery_fee_row}
        <tr style="border-top:2px solid #1a1a1a;">
          <td colspan="2" style="padding:10px 0 0 0;text-align:right;font-size:16px;font-weight:700;">Total</td>
          <td style="padding:10px 8px 0 0;text-align:right;font-size:16px;font-weight:700;">{_format_cop(total)}</td>
        </tr>
      </table>

      <!-- Payment note -->
      <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 16px;margin-bottom:24px;">
        <p style="margin:0;font-size:13px;color:#92400e;"><strong>Pago:</strong> Efectivo contra entrega</p>
      </div>

      <!-- Footer message -->
      <div style="background:#f9f9f9;border-radius:8px;padding:16px;text-align:center;">
        <p style="margin:0;font-size:14px;color:#555;">
          El restaurante está revisando tu pedido.<br>
          <strong>Te notificaremos cuando esté confirmado.</strong>
        </p>
      </div>

    </div>

    <!-- Email footer -->
    <div style="background:#f5f5f5;padding:20px 32px;border-top:1px solid #e5e5e5;">
      <p style="margin:0;font-size:11px;color:#999;text-align:center;">
        WARO Colombia · hola@warocol.com<br>
        Bogotá, D.C., Colombia
      </p>
    </div>

  </div>
</body>
</html>""".strip()


def get_order_confirmation_subject(order_number: int) -> str:
    """Email subject line for order confirmation."""
    return f"Tu pedido #{order_number} está siendo confirmado — WARO"
