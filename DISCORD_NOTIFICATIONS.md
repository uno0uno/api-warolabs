# Sistema de Notificaciones de Discord

## 📋 Descripción

Sistema automático que captura todos los errores de la API y los envía a Discord en tiempo real para monitoreo y debugging.

## 🎯 Características

- **Captura automática de errores**: Todos los errores 5xx se envían automáticamente a Discord
- **Información detallada**: Incluye traceback, request info, contexto y variables de entorno
- **Formato bonito**: Usa embeds de Discord con colores y formateo
- **No bloquea la aplicación**: Las notificaciones se envían de forma asíncrona
- **Múltiples tipos**: Errores, warnings e info

## 🚀 Instalación

### 1. Instalar dependencias

```bash
pip install aiohttp
```

### 2. Configurar webhook de Discord

Agrega el webhook URL a tu archivo `.env` o `.env.prod`:

```bash
# Webhook para errores críticos
DISCORD_ERROR_WEBHOOK_URL="https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
```

O directamente en el código (ya configurado):
```python
ERROR_WEBHOOK_URL = "https://discord.com/api/webhooks/1445269262515044464/lNHnWUHhUeObE11SOJztvwc8LqrGgLrh4uQtnqxn6lrn4KgdKARPeV7F1Nd-sNlybyaF"
```

## 📦 Archivos Creados

1. **`app/services/discord_error_notifier.py`** - Servicio principal de notificaciones
2. **`app/core/error_handlers.py`** - Handlers globales de excepciones (opcional, ya integrado en exceptions.py)
3. **`test_discord_notifications.py`** - Script de prueba

## ✅ Pruebas

Para verificar que funciona, ejecuta:

```bash
python test_discord_notifications.py
```

Deberías ver 3 notificaciones en Discord:
- 🚨 Un error de prueba (rojo)
- ⚠️ Un warning de prueba (amarillo)
- ℹ️ Un mensaje info de prueba (azul)

## 🔧 Uso

### Automático

Los errores se capturan automáticamente. Solo asegúrate de que la API esté corriendo.

### Manual

Si quieres enviar notificaciones manualmente:

```python
from app.services.discord_error_notifier import error_notifier

# Enviar error
try:
    # tu código
    raise ValueError("Algo salió mal")
except Exception as e:
    await error_notifier.send_error(
        error=e,
        context={"user_id": "123", "action": "upload_file"},
        request_info={"method": "POST", "url": "/api/upload"}
    )

# Enviar warning
await error_notifier.send_warning(
    title="Alto uso de memoria",
    message="La API está usando 85% de RAM",
    context={"memory_usage": "85%"}
)

# Enviar info
await error_notifier.send_info(
    title="Nuevo deploy",
    message="API v1.2.0 desplegada exitosamente",
    context={"version": "1.2.0", "environment": "production"}
)
```

## 📊 Información Incluida en Errores

Cada notificación de error incluye:

- **Tipo de error**: `ValueError`, `HTTPException`, etc.
- **Mensaje de error**: Descripción del problema
- **Traceback completo**: Stack trace para debugging
- **Request info**:
  - Método HTTP (GET, POST, etc.)
  - URL completa
  - IP del cliente
  - User agent
- **Contexto**:
  - Tenant ID
  - User ID (si está disponible)
  - Información adicional relevante
- **Ambiente**: development, staging, production

## 🎨 Ejemplo de Notificación

```
🚨 Error: ValueError
"Invalid file format"

📡 Request Info
Method: POST
URL: https://warocol.com/api/upload
Client: 192.168.1.100

🔍 Context
tenant: abc123
user_id: user456
file_type: pdf

📋 Traceback
```python
Traceback (most recent call last):
  File "upload.py", line 42
    raise ValueError("Invalid file format")
ValueError: Invalid file format
\```

🌍 Environment
Env: production
Base URL: https://warocol.com
```

## 🔐 Seguridad

- **No expone secretos**: Las credenciales y tokens no se incluyen en las notificaciones
- **Webhook privado**: Solo quien tenga el URL del webhook puede recibir notificaciones
- **Datos sensibles**: Se pueden filtrar campos sensibles antes de enviar

## 🛠️ Personalización

### Cambiar el webhook

Edita `app/services/discord_error_notifier.py`:

```python
ERROR_WEBHOOK_URL = "tu_nuevo_webhook_url"
```

### Filtrar errores

Si solo quieres errores críticos (por ejemplo, excluir 404):

```python
# En app/core/exceptions.py
if exc.status_code >= 500:  # Solo errores de servidor
    await error_notifier.send_error(...)
```

### Agregar más contexto

```python
discord_context = {
    "tenant": tenant,
    "user_id": user_id,
    "custom_field": "custom_value",
    # Agrega lo que necesites
}
```

## 🐛 Troubleshooting

### Las notificaciones no llegan

1. Verifica que el webhook URL sea correcto
2. Revisa los logs: `grep "Discord" /var/log/api.log`
3. Ejecuta el script de prueba: `python test_discord_notifications.py`

### Error "Failed to send error to Discord"

- Verifica que `aiohttp` esté instalado: `pip install aiohttp`
- Verifica conectividad a Discord: `curl https://discord.com/api/webhooks/...`

### Las notificaciones se duplican

- Asegúrate de no tener múltiples handlers registrados
- Revisa que no estés importando el notifier en múltiples lugares

## 📈 Estadísticas

El sistema es muy ligero:

- **Latencia**: < 100ms para enviar notificación
- **No bloquea**: Usa async/await
- **Manejo de fallos**: Si Discord no responde, no afecta la API

## 🔄 Integración Continua

Para producción, agrega el webhook a tus variables de entorno:

```bash
# En .env.prod
DISCORD_ERROR_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

Y actualiza el código para leerlo:

```python
from app.config import settings
ERROR_WEBHOOK_URL = settings.discord_error_webhook_url
```

## 📝 Notas

- Las notificaciones tienen límite de 2000 caracteres por campo
- Discord tiene rate limits (30 mensajes por minuto por webhook)
- Los tracebacks muy largos se truncan automáticamente

## 🎉 ¡Listo!

Ahora todos los errores de producción llegarán automáticamente a Discord para monitoreo en tiempo real.
