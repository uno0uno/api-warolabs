"""
Welcome email template sent when a negocio activates its profile for the first time.
"""


def get_negocio_welcome_text(display_name: str) -> str:
    return f"""¡Hola {display_name}!

Tu negocio ya está activo y visible en el directorio de WaRo Colombia.

Esto es lo que puedes hacer desde ahora:

🛵 Acepta domicilios en línea a través de tu menú público.

🍽️ Configura tu menú: activa los productos desde el módulo de Menú — eso es todo lo que necesitas hacer.

Si tienes dudas, responde a este correo y te ayudamos.

Saludos desde la nave de WaRo Colombia.

----
Saifer 101 (Anderson Arévalo)
Fundador WaRo Colombia
Dirección: Calle 39F # 68F - 66 Sur
Bogotá, D.C, Colombia
Tel: 3142047013
Correo: anderson.arevalo@warocol.com
Tecnología colombiana para el mundo.
"""


def get_negocio_welcome_subject(display_name: str) -> str:
    return f"🎉 {display_name} ya está activo en WaRo Colombia"
