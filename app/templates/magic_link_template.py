"""
Magic link email template for FastAPI authentication system
Compatible with warolabs.com template structure and branding
"""
from typing import Literal

EmailPurpose = Literal["login", "registration"]

_PURPOSE_COPY = {
    "login": {
        "intro": "Has solicitado acceso a tu cuenta en {brand_name}. Haz clic en el siguiente enlace para ingresar de forma segura:",
        "button": "🔑 Acceder a mi cuenta",
    },
    "registration": {
        "intro": "Has iniciado tu registro en {brand_name}. Haz clic en el siguiente enlace para completarlo de forma segura:",
        "button": "✨ Completar mi registro",
    },
}

def get_magic_link_template(magic_link_url: str, verification_code: str, tenant_context: dict, purpose: EmailPurpose = "login") -> str:
    """
    Generate magic link email template with dynamic tenant branding
    Compatible with warolabs.com getMagicLinkTemplate function
    
    Args:
        magic_link_url: The complete magic link URL for authentication
        verification_code: 6-digit verification code
        tenant_context: Tenant configuration with branding information
        purpose: "login" (default) or "registration" — selects subject/body copy
    
    Returns:
        HTML email template string
    """
    # Extract tenant configuration with defaults
    brand_name = tenant_context.get('brand_name', 'Waro Colombia')
    tenant_name = tenant_context.get('tenant_name', 'Waro Colombia')
    admin_name = tenant_context.get('admin_name', 'Saifer 101 (Anderson Arévalo)')
    admin_email = tenant_context.get('admin_email', 'anderson.arevalo@warolabs.com')
    copy = _PURPOSE_COPY.get(purpose, _PURPOSE_COPY["login"])
    intro = copy["intro"].format(brand_name=brand_name)
    button_label = copy["button"]
    
    # Dynamic footer message based on tenant
    footer_message = 'Tecnología colombiana para el mundo. warocol.com' if tenant_name == 'Waro Colombia' else 'No olvides mirar al futuro.'
    
    return f"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Link Mágico - {brand_name}</title>
</head>
<body style="font-family: Arial, sans-serif; color: black; margin: 0; padding: 0; text-align: left;">
    <div style="font-family: Arial, sans-serif; max-width: 600px; padding: 20px; border: 1px solid #ddd; border-radius: 8px;">
        <p>¡Hola!</p>
        
        <p>{intro}</p>
        
        <p><a href="{magic_link_url}" style="color: black; background-color: #f0f0f0; padding: 10px; border-radius: 4px; text-decoration: none; display: inline-block;">{button_label}</a></p>

        <p><strong>O usa este código de verificación:</strong></p>
        <p style="font-size: 28px; font-weight: bold; letter-spacing: 4px; color: #333; background-color: #f8f8f8; padding: 15px; border-radius: 8px; text-align: center; margin: 20px 0;">{verification_code}</p>

        <p>Este enlace es válido por 15 minutos y solo puede ser usado una vez.</p>
        
        <p>Si no solicitaste este enlace, puedes ignorar este correo de forma segura.</p>
        
        <p>Saludos desde la nave de {tenant_name}.</p>
        
        <br><br>
        ----<br>
        {admin_name}<br>
        Fundador {tenant_name}<br>
        Bogotá, D.C, Colombia<br>
        Tel: 3142047013<br>
        Correo: <a href="mailto:{admin_email}">{admin_email}</a><br>
        {footer_message}
    </div>
</body>
</html>
    """.strip()

def get_magic_link_subject(brand_name: str, purpose: EmailPurpose = "login") -> str:
    """Generate email subject line for magic link"""
    if purpose == "registration":
        return f"✨ Completa tu registro en {brand_name}"
    return f"🔑 Tu acceso a {brand_name} está listo"
