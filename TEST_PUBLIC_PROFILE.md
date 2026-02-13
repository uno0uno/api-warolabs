# Testing Public Restaurant Profile - Phase 1

## ✅ Implementación Completada

### Archivos Creados

#### Base de Datos
- ✅ `migrations/001_create_tenant_public_profiles.sql` - Tabla para perfiles públicos
- ✅ `migrations/002_insert_sample_tenant_profile.sql` - Perfil de ejemplo para Waro Colombia

#### Backend (Models)
- ✅ `app/models/tenant_public_profile.py` - Modelos Pydantic

#### Backend (Services)
- ✅ `app/services/public_restaurant_service.py` - Lógica pública (sin auth)
- ✅ `app/services/tenant_config_service.py` - Lógica admin (con auth)

#### Backend (Routers)
- ✅ `app/routers/public_restaurant.py` - Endpoints públicos
- ✅ `app/routers/tenant_config.py` - Endpoints admin

#### Configuración
- ✅ `app/main.py` - Routers registrados
- ✅ `app/routers/__init__.py` - Exports actualizados

---

## 🧪 Pruebas de Endpoints

### Base URL
```
Development: http://localhost:9999
Production: https://api.warolabs.com
```

---

## 📡 Endpoints Públicos (Sin Autenticación)

### 1. Obtener Perfil Público

**Endpoint:** `GET /api/public/restaurant/{tenant_slug}`

**Ejemplo:**
```bash
curl http://localhost:9999/api/public/restaurant/waro-colombia
```

**Respuesta Esperada:**
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "tenant_id": "93b3e582-34fa-44a6-8d0f-bf82a3608727",
    "slug": "waro-colombia",
    "is_active": true,
    "display_name": "Waro Colombia",
    "description": "Restaurante de comida colombiana...",
    "logo_url": "🍔",
    "banner_url": "🏪",
    "phone_number": "+57 320 1234567",
    "email": "contacto@warocolombia.com",
    "address": "Calle 123 #45-67, Local 101",
    "city": "Bogotá",
    "neighborhood": "Chapinero",
    "latitude": 4.60971,
    "longitude": -74.08175,
    "business_hours": {
      "monday": {"open": "09:00", "close": "22:00", "closed": false},
      "tuesday": {"open": "09:00", "close": "22:00", "closed": false},
      "wednesday": {"open": "09:00", "close": "22:00", "closed": false},
      "thursday": {"open": "09:00", "close": "22:00", "closed": false},
      "friday": {"open": "09:00", "close": "23:00", "closed": false},
      "saturday": {"open": "10:00", "close": "23:00", "closed": false},
      "sunday": {"closed": true}
    },
    "social_media": {
      "facebook": "https://facebook.com/warocolombia",
      "instagram": "@warocolombia",
      "whatsapp": "+573201234567"
    },
    "seo_title": "Waro Colombia - Comida Típica Colombiana",
    "seo_description": "Restaurante de comida colombiana...",
    "accepts_online_orders": false,
    "min_order_amount": 0,
    "estimated_preparation_time": 30,
    "is_currently_open": true,
    "created_at": "2026-02-06T14:45:19.162225",
    "updated_at": "2026-02-06T14:45:19.162225"
  }
}
```

**Caso de Error:**
```bash
curl http://localhost:9999/api/public/restaurant/restaurant-no-existe
```

```json
{
  "detail": "Restaurant not found or not active"
}
```

---

### 2. Obtener Menú Público

**Endpoint:** `GET /api/public/restaurant/{tenant_slug}/menu`

**Ejemplo (todos los productos):**
```bash
curl http://localhost:9999/api/public/restaurant/waro-colombia/menu
```

**Ejemplo (filtrado por categoría):**
```bash
curl "http://localhost:9999/api/public/restaurant/waro-colombia/menu?category_id=<uuid>"
```

**Respuesta Esperada:**
```json
{
  "success": true,
  "data": {
    "restaurant_name": "Waro Colombia",
    "categories": [
      {
        "id": "uuid",
        "name": "Hamburguesas",
        "description": "Nuestras hamburguesas artesanales"
      },
      {
        "id": "uuid",
        "name": "Bebidas",
        "description": "Bebidas frías y calientes"
      }
    ],
    "products": [
      {
        "id": "uuid",
        "name": "Hamburguesa Clásica",
        "description": "Carne 100% res, lechuga, tomate",
        "price": "15000.00",
        "category_id": "uuid",
        "category_name": "Hamburguesas",
        "is_available": true,
        "preparation_time": 15,
        "allow_modifiers": true,
        "has_modifiers": true
      }
    ]
  }
}
```

---

### 3. Obtener Detalle de Producto

**Endpoint:** `GET /api/public/restaurant/{tenant_slug}/product/{product_id}`

**Ejemplo:**
```bash
curl http://localhost:9999/api/public/restaurant/waro-colombia/product/<product_uuid>
```

**Respuesta Esperada:**
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "name": "Hamburguesa Clásica",
    "description": "Carne 100% res, lechuga, tomate, cebolla",
    "price": "15000.00",
    "category_name": "Hamburguesas",
    "is_available": true,
    "preparation_time": 15,
    "modifier_groups": [
      {
        "id": "uuid",
        "name": "Tamaño",
        "is_required": true,
        "min_qty": 1,
        "max_qty": 1,
        "modifiers": [
          {
            "id": "uuid",
            "name": "Simple",
            "price": 0,
            "is_available": true
          },
          {
            "id": "uuid",
            "name": "Doble Carne",
            "price": 8000,
            "is_available": true
          }
        ]
      },
      {
        "id": "uuid",
        "name": "Extras",
        "is_required": false,
        "min_qty": 0,
        "max_qty": 5,
        "modifiers": [
          {
            "id": "uuid",
            "name": "Extra Queso",
            "price": 2000,
            "is_available": true
          }
        ]
      }
    ]
  }
}
```

---

## 🔒 Endpoints Admin (Requiere Autenticación)

**Nota:** Todos estos endpoints requieren una sesión válida con cookie `session-token`.

### 4. Ver Propio Perfil Público

**Endpoint:** `GET /api/tenant/public-profile`

**Ejemplo:**
```bash
curl http://localhost:9999/api/tenant/public-profile \
  -H "Cookie: session-token=YOUR_SESSION_TOKEN"
```

**Respuesta:** Mismo formato que endpoint público, pero muestra perfil del tenant autenticado.

---

### 5. Crear/Actualizar Perfil Público (Reemplazo Completo)

**Endpoint:** `PUT /api/tenant/public-profile`

**Ejemplo:**
```bash
curl -X PUT http://localhost:9999/api/tenant/public-profile \
  -H "Content-Type: application/json" \
  -H "Cookie: session-token=YOUR_SESSION_TOKEN" \
  -d '{
    "tenant_id": "93b3e582-34fa-44a6-8d0f-bf82a3608727",
    "slug": "waro-colombia",
    "is_active": true,
    "display_name": "Waro Colombia Actualizado",
    "description": "Nueva descripción del restaurante",
    "logo_url": "https://ejemplo.com/logo.png",
    "banner_url": "https://ejemplo.com/banner.jpg",
    "phone_number": "+57 320 9876543",
    "email": "nuevo@warocolombia.com",
    "address": "Nueva dirección 456",
    "city": "Bogotá",
    "neighborhood": "Usaquén",
    "latitude": 4.7110,
    "longitude": -74.0721,
    "business_hours": {
      "monday": {"open": "10:00", "close": "21:00", "closed": false},
      "tuesday": {"open": "10:00", "close": "21:00", "closed": false},
      "wednesday": {"open": "10:00", "close": "21:00", "closed": false},
      "thursday": {"open": "10:00", "close": "21:00", "closed": false},
      "friday": {"open": "10:00", "close": "22:00", "closed": false},
      "saturday": {"open": "11:00", "close": "22:00", "closed": false},
      "sunday": {"closed": true}
    },
    "social_media": {
      "facebook": "https://facebook.com/warocol",
      "instagram": "@warocol",
      "whatsapp": "+573209876543",
      "twitter": "@warocol"
    },
    "seo_title": "Waro Colombia - Los Mejores Platos",
    "seo_description": "Nueva descripción SEO",
    "accepts_online_orders": false,
    "min_order_amount": 0,
    "estimated_preparation_time": 25
  }'
```

---

### 6. Actualizar Perfil Público (Parcial)

**Endpoint:** `PATCH /api/tenant/public-profile`

**Ejemplo (solo actualizar descripción y teléfono):**
```bash
curl -X PATCH http://localhost:9999/api/tenant/public-profile \
  -H "Content-Type: application/json" \
  -H "Cookie: session-token=YOUR_SESSION_TOKEN" \
  -d '{
    "description": "Nueva descripción corta",
    "phone_number": "+57 320 1111111"
  }'
```

**Nota:** Solo se actualizan los campos enviados.

---

### 7. Activar/Desactivar Perfil Público

**Endpoint:** `POST /api/tenant/public-profile/toggle`

**Ejemplo (activar):**
```bash
curl -X POST http://localhost:9999/api/tenant/public-profile/toggle \
  -H "Content-Type: application/json" \
  -H "Cookie: session-token=YOUR_SESSION_TOKEN" \
  -d '{"is_active": true}'
```

**Ejemplo (desactivar):**
```bash
curl -X POST http://localhost:9999/api/tenant/public-profile/toggle \
  -H "Content-Type: application/json" \
  -H "Cookie: session-token=YOUR_SESSION_TOKEN" \
  -d '{"is_active": false}'
```

**Respuesta:**
```json
{
  "success": true,
  "message": "Public profile activated successfully",
  "is_active": true
}
```

---

## 🔍 Verificaciones en Base de Datos

### Ver todos los perfiles públicos
```sql
SELECT
  id,
  tenant_id,
  slug,
  is_active,
  display_name,
  phone_number,
  email,
  city
FROM tenant_public_profiles
ORDER BY created_at DESC;
```

### Ver perfil con información del tenant
```sql
SELECT
  tpp.slug,
  tpp.display_name,
  tpp.is_active,
  tpp.phone_number,
  t.name as tenant_name,
  t.slug as tenant_slug
FROM tenant_public_profiles tpp
JOIN tenants t ON t.id = tpp.tenant_id
WHERE tpp.is_active = true;
```

### Ver productos disponibles para un tenant
```sql
SELECT
  p.id,
  p.name,
  p.price,
  c.name as category,
  p.is_available
FROM product p
JOIN categories c ON c.id = p.category_id
WHERE p.tenant_id = '93b3e582-34fa-44a6-8d0f-bf82a3608727'
AND p.is_available = true
ORDER BY c.name, p.name;
```

---

## 📊 Estado de Implementación

### ✅ Completado (Fase 1)
- [x] Tabla `tenant_public_profiles` creada
- [x] Modelos Pydantic implementados
- [x] Servicios backend (público y admin)
- [x] Routers REST API
- [x] Endpoints públicos funcionales
- [x] Endpoints admin funcionales
- [x] Validación de slugs únicos
- [x] Cálculo de "is_currently_open"
- [x] Perfil de ejemplo insertado

### 🔄 Pendiente (Fases Futuras)
- [ ] Frontend público: Página `/{tenant_slug}`
- [ ] Frontend admin: Configuración de perfil en panel
- [ ] Componente de horarios de negocio
- [ ] Upload de imágenes (logo/banner)
- [ ] SEO: Meta tags y Open Graph
- [ ] Testing end-to-end

---

## 🚀 Próximos Pasos

1. **Iniciar el servidor de desarrollo:**
   ```bash
   cd "/Users/saifer/Documents/WEBS/WARO COLOMBIA/api_warocol.com"
   source venv/bin/activate
   uvicorn app.main:app --reload --port 9999
   ```

2. **Probar endpoints públicos:**
   - Abrir http://localhost:9999/docs
   - Probar `GET /api/public/restaurant/waro-colombia`
   - Probar `GET /api/public/restaurant/waro-colombia/menu`

3. **Crear más perfiles de prueba** (opcional):
   - Duplicar `002_insert_sample_tenant_profile.sql`
   - Cambiar tenant_id y slug para otros restaurants
   - Ejecutar migración

4. **Desarrollar Frontend:**
   - Crear página pública `pages/[tenant]/index.vue`
   - Mostrar información del restaurante
   - Mostrar menú con productos
   - Agregar diseño responsive

---

## 📝 Notas Importantes

### Seguridad
- ✅ Endpoints públicos NO requieren autenticación
- ✅ Endpoints admin requieren sesión válida
- ✅ Slugs son únicos (validación en backend)
- ✅ Solo perfiles con `is_active=true` son visibles públicamente

### Performance
- ✅ Índices creados en `slug`, `tenant_id`, `is_active`
- ✅ Query de menú incluye JOIN optimizado con categorías
- ⚠️ Considerar cache (Redis) para perfiles públicos en producción

### Escalabilidad
- ✅ Multi-tenant: Cada restaurant tiene su propio perfil
- ✅ JSONB para datos flexibles (horarios, redes sociales)
- ✅ Preparado para futuras fases (pedidos online, delivery)
