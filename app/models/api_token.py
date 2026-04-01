from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
from uuid import UUID


class ApiTokenCreate(BaseModel):
    """Request body para crear un nuevo API token"""
    name: str = Field(..., min_length=1, max_length=100, description="Nombre descriptivo del token")
    scopes: List[str] = Field(default=["read"], description="Permisos del token")
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365, description="Dias hasta expirar (null = no expira)")


class ApiTokenCreatedResponse(BaseModel):
    """Response cuando se crea un token - incluye el token completo (unica vez que se muestra)"""
    success: bool = True
    message: str = "API token created successfully"
    data: "ApiTokenWithSecret"


class ApiTokenWithSecret(BaseModel):
    """Token recien creado con el secret visible (solo se muestra una vez)"""
    id: UUID
    name: str
    key_prefix: str = Field(alias="keyPrefix")
    secret_key: str = Field(alias="secretKey", description="Token completo - guardar de forma segura, no se mostrara de nuevo")
    scopes: List[str]
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    created_at: datetime = Field(alias="createdAt")

    class Config:
        populate_by_name = True


class ApiToken(BaseModel):
    """Token sin el secret (para listados)"""
    id: UUID
    name: str
    key_prefix: str = Field(alias="keyPrefix")
    scopes: List[str]
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    last_used_at: Optional[datetime] = Field(None, alias="lastUsedAt")
    created_at: datetime = Field(alias="createdAt")
    is_active: bool = Field(alias="isActive")
    created_by_name: Optional[str] = Field(None, alias="createdByName")

    class Config:
        populate_by_name = True


class ApiTokenListResponse(BaseModel):
    """Response para listar tokens"""
    success: bool = True
    data: List[ApiToken]


class ApiTokenDeleteResponse(BaseModel):
    """Response al eliminar/revocar un token"""
    success: bool = True
    message: str


class ApiTokenUpdateRequest(BaseModel):
    """Request para actualizar un token"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    scopes: Optional[List[str]] = None
    is_active: Optional[bool] = Field(None, alias="isActive")

    class Config:
        populate_by_name = True


class ApiTokenUpdateResponse(BaseModel):
    """Response al actualizar un token"""
    success: bool = True
    message: str
    data: ApiToken


# Scopes disponibles
AVAILABLE_SCOPES = [
    "read",            # Lectura general (cubre todos los *:read)
    "write",           # Escritura general (cubre todos los *:write)
    "orders:read",     # Leer ordenes
    "orders:write",    # Crear/modificar ordenes
    "menu:read",       # Leer menu (productos, recetas, modificadores)
    "products:read",   # Leer productos
    "products:write",  # Crear/modificar productos
    "inventory:read",  # Leer inventario
    "inventory:write", # Modificar inventario
    "customers:read",  # Leer clientes
    "customers:write", # Crear/modificar clientes
    "analytics:read",  # Leer analytics (menu BCG, food cost, alertas, calidad de datos)
    "financial:read",  # Leer financiero (analisis de productos)
    "waros:read",      # Leer waros (estimados, balances, resumen de cliente)
]
