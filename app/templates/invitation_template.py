"""
Team invitation email template for FastAPI authentication system
Compatible with warolabs.com template structure and branding
"""

def get_invitation_template(invitation_url: str, context: dict) -> str:
    """
    Generate team invitation email template with dynamic tenant branding

    Args:
        invitation_url: The complete invitation URL for accepting
        context: Template context with branding and invitation information

    Returns:
        HTML email template string
    """
    brand_name = context.get('brand_name', 'Waro Colombia')
    tenant_name = context.get('tenant_name', 'Waro Colombia')
    inviter_name = context.get('inviter_name', 'Un administrador')
    invitee_name = context.get('invitee_name', '')
    role = context.get('role', 'Administrador')

    # Dynamic footer message based on tenant
    footer_message = 'Tecnología colombiana para el mundo. warocol.com' if tenant_name == 'Waro Colombia' else 'No olvides mirar al futuro.'

    greeting = f"Hola {invitee_name}!" if invitee_name else "Hola!"

    return f"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Invitacion al equipo - {brand_name}</title>
</head>
<body style="font-family: Arial, sans-serif; color: black; margin: 0; padding: 0; text-align: left;">
    <div style="font-family: Arial, sans-serif; max-width: 600px; padding: 20px; border: 1px solid #ddd; border-radius: 8px;">
        <p>{greeting}</p>

        <p><strong>{inviter_name}</strong> te ha invitado a unirte al equipo de <strong>{tenant_name}</strong> como <strong>{role}</strong>.</p>

        <p>Haz clic en el siguiente enlace para aceptar la invitacion y comenzar:</p>

        <p style="text-align: center; margin: 30px 0;">
            <a href="{invitation_url}" style="color: white; background-color: #2563eb; padding: 14px 28px; border-radius: 6px; text-decoration: none; display: inline-block; font-weight: bold;">
                Aceptar invitacion
            </a>
        </p>

        <p style="color: #666; font-size: 14px;">Este enlace es valido por 7 dias.</p>

        <p>Si no esperabas esta invitacion, puedes ignorar este correo de forma segura.</p>

        <p>Saludos desde la nave de {tenant_name}.</p>

        <br><br>
        ----<br>
        Equipo {tenant_name}<br>
        Bogota, D.C, Colombia<br>
        Tel: 3142047013<br>
        {footer_message}
    </div>
</body>
</html>
    """.strip()


def get_invitation_subject(brand_name: str) -> str:
    """Generate email subject line for team invitation"""
    return f"Te han invitado a unirte a {brand_name}"
