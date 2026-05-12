from fastapi import APIRouter, Depends, Request
from app.core.permissions import Module, require_module
from app.services.api_tokens_service import (
    create_api_token,
    list_api_tokens,
    revoke_api_token,
    delete_api_token,
    update_api_token
)
from app.models.api_token import (
    ApiTokenCreate,
    ApiTokenCreatedResponse,
    ApiTokenListResponse,
    ApiTokenDeleteResponse,
    ApiTokenUpdateRequest,
    ApiTokenUpdateResponse,
    AVAILABLE_SCOPES
)

router = APIRouter()


@router.post("", response_model=ApiTokenCreatedResponse, dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def create_api_token_endpoint(request: Request, body: ApiTokenCreate):
    """
    Crea un nuevo API token para el tenant actual.

    El token completo solo se muestra una vez en la respuesta.
    Guarda el secretKey de forma segura - no se puede recuperar despues.

    Requiere rol: admin o superuser
    """
    return await create_api_token(request, body)


@router.get("", response_model=ApiTokenListResponse, dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def list_api_tokens_endpoint(request: Request):
    """
    Lista todos los API tokens del tenant actual.

    Solo muestra el prefijo del token (keyPrefix), no el token completo.
    """
    return await list_api_tokens(request)


@router.get("/scopes", dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def get_available_scopes():
    """
    Retorna la lista de scopes disponibles para API tokens.
    """
    return {
        "success": True,
        "data": AVAILABLE_SCOPES,
        "descriptions": {
            "read": "Lectura general de datos",
            "write": "Escritura general de datos",
            "orders:read": "Leer ordenes",
            "orders:write": "Crear y modificar ordenes",
            "products:read": "Leer productos",
            "products:write": "Crear y modificar productos",
            "inventory:read": "Leer inventario",
            "inventory:write": "Modificar inventario",
            "customers:read": "Leer clientes",
            "customers:write": "Crear y modificar clientes"
        }
    }


@router.patch("/{token_id}", response_model=ApiTokenUpdateResponse, dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def update_api_token_endpoint(request: Request, token_id: str, body: ApiTokenUpdateRequest):
    """
    Actualiza un API token (nombre, scopes, estado activo).

    Requiere rol: admin o superuser
    """
    return await update_api_token(
        request,
        token_id,
        name=body.name,
        scopes=body.scopes,
        is_active=body.is_active
    )


@router.post("/{token_id}/revoke", response_model=ApiTokenDeleteResponse, dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def revoke_api_token_endpoint(request: Request, token_id: str):
    """
    Revoca (desactiva) un API token.

    El token no se elimina, solo se marca como inactivo.
    Los requests con este token seran rechazados.

    Requiere rol: admin o superuser
    """
    return await revoke_api_token(request, token_id)


@router.delete("/{token_id}", response_model=ApiTokenDeleteResponse, dependencies=[Depends(require_module(Module.INTEGRACIONES))])
async def delete_api_token_endpoint(request: Request, token_id: str):
    """
    Elimina permanentemente un API token.

    Esta accion no se puede deshacer.

    Requiere rol: admin o superuser
    """
    return await delete_api_token(request, token_id)
