"""Resolve Matias DIAN environment label per tenant (mirrors api_facturacion)."""
from __future__ import annotations

from typing import Optional, Set, Union
from uuid import UUID

from app.config import settings

_HABILITACION_ENV = 2


def _habilitacion_tenant_keys() -> Set[str]:
    raw = (settings.matias_habilitacion_tenant_ids or "").strip()
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _sandbox_tenant_keys() -> Set[str]:
    raw = (settings.matias_sandbox_tenant_ids or "").strip()
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _tenant_in_keys(
    tenant_id: Union[str, UUID],
    keys: Set[str],
    tenant_slug: Optional[str] = None,
) -> bool:
    if not keys:
        return False
    tenant_key = str(tenant_id).lower()
    if tenant_key in keys:
        return True
    return bool(tenant_slug and tenant_slug.lower() in keys)


def matias_sandbox_for_tenant(
    tenant_id: Union[str, UUID],
    tenant_slug: Optional[str] = None,
) -> bool:
    """True when this tenant is routed through the Matias sandbox host."""
    return _tenant_in_keys(tenant_id, _sandbox_tenant_keys(), tenant_slug)


def matias_environment_for_tenant(
    tenant_id: Union[str, UUID],
    tenant_slug: Optional[str] = None,
) -> int:
    if _tenant_in_keys(tenant_id, _habilitacion_tenant_keys(), tenant_slug):
        return _HABILITACION_ENV
    return settings.matias_environment_id


def matias_environment_label(environment_id: int) -> str:
    if environment_id == _HABILITACION_ENV:
        return "Habilitación (pruebas)"
    return "Producción"
